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
            self.current_index = random.randint(0, len(self.tracks) - 1)
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
                track = Track(
                    url=info.get("webpage_url", info.get("url", url)),
                    title=info.get("title", "Unknown"),
                    artist=info.get("uploader", info.get("artist", "")),
                    duration=info.get("duration", 0) or 0,
                    source=info.get("extractor", "unknown"),
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


async def get_stream_url(url: str) -> str:
    """获取单个 URL 的直接流播放地址"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "--no-download",
            "--get-url",
            "-f", "bestaudio/best",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0 and stdout.strip():
            return stdout.decode().strip().split("\n")[0]
    except (FileNotFoundError, asyncio.TimeoutError):
        pass
    return url


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
