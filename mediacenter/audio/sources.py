"""音乐源解析模块 - 支持多种音乐平台"""

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class PlayMode(str, Enum):
    SEQUENTIAL = "sequential"
    SHUFFLE = "shuffle"
    REPEAT_ONE = "repeat_one"
    REPEAT_ALL = "repeat_all"


@dataclass
class Track:
    """音轨信息"""
    url: str
    title: str = ""
    artist: str = ""
    duration: float = 0.0
    source: str = "unknown"
    # yt-dlp 解析后的直接播放 URL
    playback_url: str = ""
    # 拉 playback_url 时要带的 HTTP 头(yt-dlp 给的 http_headers;B 站直链缺 Referer 会 403)
    http_headers: dict = field(default_factory=dict)

    @property
    def effective_url(self) -> str:
        return self.playback_url or self.url


@dataclass
class Playlist:
    """播放列表"""
    name: str = "default"
    tracks: list[Track] = field(default_factory=list)
    current_index: int = 0
    play_mode: PlayMode = PlayMode.REPEAT_ALL

    def next_track(self) -> Track | None:
        if not self.tracks:
            return None

        if self.play_mode == PlayMode.REPEAT_ONE:
            return self.tracks[self.current_index]
        elif self.play_mode == PlayMode.SHUFFLE:
            # 多于一首时别连着抽到同一首
            candidates = [i for i in range(len(self.tracks)) if i != self.current_index]
            self.current_index = random.choice(candidates or [self.current_index])
        else:
            self.current_index += 1
            if self.current_index >= len(self.tracks):
                if self.play_mode == PlayMode.REPEAT_ALL:
                    self.current_index = 0
                else:
                    return None  # sequential 播完结束

        return self.tracks[self.current_index]

    def prev_track(self) -> Track | None:
        if not self.tracks:
            return None
        self.current_index = (self.current_index - 1) % len(self.tracks)
        return self.tracks[self.current_index]

    @property
    def current_track(self) -> Track | None:
        if 0 <= self.current_index < len(self.tracks):
            return self.tracks[self.current_index]
        return None


async def resolve_url(url: str) -> list[Track]:
    """使用 yt-dlp 解析 URL 获取可播放的音频流地址。

    支持 YouTube、Bilibili、网易云音乐等 yt-dlp 支持的所有平台。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "--no-download",
            "--dump-json",
            "--flat-playlist",
            "--no-warnings",
            "-f", "bestaudio/best",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            logger.warning(f"yt-dlp 解析失败: {stderr.decode()[:200]}")
            # 解析失败则直接当作流地址
            return [Track(url=url, title=url, source="direct")]

        tracks = []
        for line in stdout.decode().strip().split("\n"):
            if not line:
                continue
            try:
                info = json.loads(line)
                # generic 提取器碰到裸媒体文件(navidrome 的 stream.view 之类)会标
                # direct=True:它没有"网页 → 流地址"这一步,播放时不必再跑一次 yt-dlp
                source = "direct" if info.get("direct") else info.get("extractor", "unknown")
                track = Track(
                    url=info.get("webpage_url", info.get("url", url)),
                    title=info.get("title", "Unknown"),
                    artist=info.get("uploader", info.get("artist", "")),
                    duration=info.get("duration", 0) or 0,
                    source=source,
                )
                tracks.append(track)
            except json.JSONDecodeError:
                continue

        return tracks if tracks else [Track(url=url, title=url, source="direct")]

    except FileNotFoundError:
        logger.warning("yt-dlp 未安装，直接使用 URL")
        return [Track(url=url, title=url, source="direct")]
    except asyncio.TimeoutError:
        logger.warning("yt-dlp 解析超时")
        return [Track(url=url, title=url, source="direct")]


async def resolve_stream(url: str) -> tuple[str, dict]:
    """获取单个 URL 的直接流播放地址,连同拉流要带的 HTTP 头。

    用 --dump-json 而不是 --get-url:后者只吐 URL,把 yt-dlp 算好的
    http_headers 丢了 —— B 站的直链没有 Referer 直接 403(mpv 报 loading failed)。
    解析不了就原样返回,让 mpv 自己试。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "--no-download",
            "--dump-json",
            "--no-playlist",
            "--no-warnings",
            "-f", "bestaudio/best",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0 and stdout.strip():
            info = json.loads(stdout.decode().strip().split("\n")[0])
            stream_url = info.get("url") or url
            headers = info.get("http_headers") or {}
            return stream_url, dict(headers)
        logger.warning(f"yt-dlp 解析流地址失败: {stderr.decode()[:200]}")
    except (FileNotFoundError, asyncio.TimeoutError, json.JSONDecodeError) as e:
        logger.warning(f"yt-dlp 解析流地址失败: {e!r}")
    return url, {}


async def get_stream_url(url: str) -> str:
    """兼容旧接口:只要直接流地址,不要头"""
    stream_url, _ = await resolve_stream(url)
    return stream_url


async def search_music(query: str, source: str = "youtube") -> list[Track]:
    """搜索音乐

    Args:
        query: 搜索关键词
        source: 搜索源 (youtube / bilibili)
    """
    search_prefix = {
        "youtube": "ytsearch5:",
        "bilibili": "bilisearch5:",
    }
    prefix = search_prefix.get(source, "ytsearch5:")

    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "--no-download",
            "--dump-json",
            "--flat-playlist",
            "--no-warnings",
            f"{prefix}{query}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)

        tracks = []
        for line in stdout.decode().strip().split("\n"):
            if not line:
                continue
            try:
                info = json.loads(line)
                tracks.append(Track(
                    url=info.get("url", info.get("webpage_url", "")),
                    title=info.get("title", "Unknown"),
                    artist=info.get("uploader", ""),
                    duration=info.get("duration", 0) or 0,
                    source=source,
                ))
            except json.JSONDecodeError:
                continue
        return tracks

    except (FileNotFoundError, asyncio.TimeoutError):
        logger.warning("搜索失败")
        return []
