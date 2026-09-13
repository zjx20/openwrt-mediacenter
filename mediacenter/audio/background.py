"""背景音乐播放器 - 管理播放列表和自动播放逻辑"""

import asyncio
import logging

from .manager import AudioManager, AudioPriority
from .player import PlayerState
from .sources import Playlist, PlayMode, Track, resolve_stream, resolve_url

logger = logging.getLogger(__name__)


class BackgroundPlayer:
    """背景音乐播放器

    负责管理播放列表，处理自动切歌逻辑。
    TTS 播报时背景音乐不会被暂停，仅由 PulseAudio ducking 自动压低音量。
    """

    # 连续这么多首都放不出来就停下,别在坏歌单上无限空转
    # (repeat_all + 全部出错 = 每秒一次 yt-dlp 的死循环)
    MAX_CONSECUTIVE_ERRORS = 5

    def __init__(self, manager: AudioManager):
        self.manager = manager
        self.playlist = Playlist()
        self._task: asyncio.Task | None = None
        self._playing = False
        self._consecutive_errors = 0

        # 监听播放结束事件，自动切下一首
        bg_player = manager.channels[AudioPriority.BACKGROUND].player
        bg_player.on_event("end-file", self._on_track_end)

    async def _on_track_end(self, event: dict):
        """当前曲目播放结束，自动播放下一首"""
        if not self._playing:
            return

        reason = event.get("reason", "")
        # 只有自然播完(eof)和出错(error)才算"这首结束了,该下一首"。
        # stop 是我们自己的 stop / loadfile replace(next、prev、插播)造成的,
        # 那时下一首已经在路上,这里再前进一次就会跳两首。
        if reason not in ("eof", "error"):
            return

        if reason == "error":
            self._consecutive_errors += 1
            logger.warning(f"播放出错(连续 {self._consecutive_errors} 次),跳到下一首")
            if self._stop_if_too_many_errors():
                return
            await asyncio.sleep(1)
        else:
            self._consecutive_errors = 0

        await self._advance()

    def _stop_if_too_many_errors(self) -> bool:
        limit = max(self.MAX_CONSECUTIVE_ERRORS, len(self.playlist.tracks))
        if self._consecutive_errors >= limit:
            self._playing = False
            logger.error(f"连续 {self._consecutive_errors} 首都放不出来,停止背景音乐")
            return True
        return False

    async def _advance(self):
        """切到下一首并播放;起播失败(解析失败、mpv 重建)就继续往后试。"""
        while True:
            next_track = self.playlist.next_track()
            if next_track is None:
                self._playing = False
                logger.info("播放列表已结束")
                return
            try:
                await self._play_track(next_track)
                return
            except Exception as e:
                self._consecutive_errors += 1
                logger.warning(f"起播失败(连续 {self._consecutive_errors} 次): {e}")
                if self._stop_if_too_many_errors():
                    return
                await asyncio.sleep(1)

    async def _play_track(self, track: Track):
        """播放单个曲目"""
        if track.source == "direct":
            # 加载时 yt-dlp 就认不出它(navidrome 之类的裸流),别每首再跑一次
            # yt-dlp(这台机器上一次一两秒,还有 30s 超时的风险)
            url, headers = track.url, {}
        else:
            url, headers = await resolve_stream(track.url)
        track.playback_url = url
        track.http_headers = headers
        await self.manager.play(AudioPriority.BACKGROUND, track.effective_url, http_headers=headers)
        logger.info(f"正在播放: {track.title} - {track.artist}")

    async def add_and_play(self, url: str):
        """添加 URL 到播放列表并立即播放"""
        tracks = await resolve_url(url)
        if not tracks:
            return

        self.playlist.tracks.extend(tracks)
        if not self._playing:
            self.playlist.current_index = len(self.playlist.tracks) - len(tracks)
            await self.start()

    # 同时跑几个 yt-dlp 解析。它是 Python 进程,这台机器上起一个要三四秒、几十 MB,
    # 串行等 6 首就是二十多秒才出声;并行 3 路把它压到十秒内,又不至于把宿主吃满
    RESOLVE_CONCURRENCY = 3

    async def load_playlist(self, urls: list[str], play_mode: str = "repeat_all"):
        """加载播放列表"""
        self.playlist = Playlist(play_mode=PlayMode(play_mode))
        sem = asyncio.Semaphore(self.RESOLVE_CONCURRENCY)

        async def resolve(url: str) -> list[Track]:
            async with sem:
                return await resolve_url(url)

        # 保持 urls 的顺序
        for tracks in await asyncio.gather(*(resolve(u) for u in urls)):
            self.playlist.tracks.extend(tracks)
        logger.info(f"播放列表已加载: {len(self.playlist.tracks)} 首")

    async def start(self):
        """开始播放"""
        track = self.playlist.current_track
        if track:
            self._playing = True
            self._consecutive_errors = 0
            await self._play_track(track)

    async def stop(self):
        """停止播放"""
        self._playing = False
        await self.manager.stop(AudioPriority.BACKGROUND)

    async def next(self):
        """下一首"""
        track = self.playlist.next_track()
        if track:
            await self._play_track(track)
        else:
            await self.stop()

    async def prev(self):
        """上一首"""
        track = self.playlist.prev_track()
        if track:
            await self._play_track(track)

    async def pause(self):
        """暂停"""
        await self.manager.pause(AudioPriority.BACKGROUND)

    async def resume(self):
        """恢复。暂停的就解除暂停;停住的(播完了 / 出错停了)从当前曲目重新开。"""
        bg = self.manager.channels[AudioPriority.BACKGROUND].player
        if bg.state == PlayerState.PAUSED:
            await self.manager.resume(AudioPriority.BACKGROUND)
        elif bg.state == PlayerState.STOPPED and self.playlist.tracks:
            if self.playlist.current_track is None:
                # 顺序模式播到了头(index == len),从第一首重新来
                self.playlist.current_index = 0
            await self.start()

    def set_play_mode(self, mode: str):
        """设置播放模式"""
        self.playlist.play_mode = PlayMode(mode)

    def get_status(self) -> dict:
        bg = self.manager.channels[AudioPriority.BACKGROUND]
        track = self.playlist.current_track
        return {
            "playing": self._playing,
            "state": bg.player.state.name,
            "play_mode": self.playlist.play_mode.value,
            "current_track": {
                "title": track.title,
                "artist": track.artist,
                "url": track.url,
                "source": track.source,
            } if track else None,
            "playlist_length": len(self.playlist.tracks),
            "current_index": self.playlist.current_index,
        }
