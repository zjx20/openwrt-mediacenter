"""背景音乐播放器 - 管理播放列表和自动播放逻辑"""

import asyncio
import logging

from .manager import AudioManager, AudioPriority
from .player import PlayerState
from .sources import Playlist, PlayMode, Track, get_stream_url, resolve_url

logger = logging.getLogger(__name__)


class BackgroundPlayer:
    """背景音乐播放器

    负责管理播放列表，处理自动切歌逻辑。
    TTS 播报时背景音乐不会被暂停，仅由 PulseAudio ducking 自动压低音量。
    """

    def __init__(self, manager: AudioManager):
        self.manager = manager
        self.playlist = Playlist()
        self._task: asyncio.Task | None = None
        self._playing = False

        # 监听播放结束事件，自动切下一首
        bg_player = manager.channels[AudioPriority.BACKGROUND].player
        bg_player.on_event("end-file", self._on_track_end)

    async def _on_track_end(self, event: dict):
        """当前曲目播放结束，自动播放下一首"""
        if not self._playing:
            return

        reason = event.get("reason", "")
        if reason == "error":
            logger.warning("播放出错，跳到下一首")

        next_track = self.playlist.next_track()
        if next_track:
            await self._play_track(next_track)
        else:
            self._playing = False
            logger.info("播放列表已结束")

    async def _play_track(self, track: Track):
        """播放单个曲目"""
        url = await get_stream_url(track.url)
        track.playback_url = url
        await self.manager.play(AudioPriority.BACKGROUND, track.effective_url)
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

    async def load_playlist(self, urls: list[str], play_mode: str = "repeat_all"):
        """加载播放列表"""
        self.playlist = Playlist(play_mode=PlayMode(play_mode))
        for url in urls:
            tracks = await resolve_url(url)
            self.playlist.tracks.extend(tracks)
        logger.info(f"播放列表已加载: {len(self.playlist.tracks)} 首")

    async def start(self):
        """开始播放"""
        track = self.playlist.current_track
        if track:
            self._playing = True
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
        """恢复"""
        await self.manager.resume(AudioPriority.BACKGROUND)

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
