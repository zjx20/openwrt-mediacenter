"""优先级音频管理器

优先级体系:
  - BACKGROUND (0): 背景音乐，最低优先级
  - STREAM (1): DLNA/AirPlay 推流，中等优先级
  - TTS (2): TTS 播报，最高优先级

高优先级音频播放时，低优先级音频自动暂停；高优先级结束后，自动恢复。
"""

import asyncio
import logging
from enum import IntEnum
from typing import Callable, Awaitable

from .player import MpvPlayer, PlayerState

logger = logging.getLogger(__name__)


class AudioPriority(IntEnum):
    BACKGROUND = 0  # 背景音乐
    STREAM = 1  # DLNA / AirPlay 推流
    TTS = 2  # TTS 播报


class AudioChannel:
    """一个优先级通道"""

    def __init__(self, priority: AudioPriority, player: MpvPlayer):
        self.priority = priority
        self.player = player
        self.was_playing = False  # 被高优先级打断前是否在播放


class AudioManager:
    """多优先级音频管理器"""

    def __init__(self, backend: str = "pulse", pulse_sink: str = ""):
        self.backend = backend
        self.pulse_sink = pulse_sink
        self.channels: dict[AudioPriority, AudioChannel] = {}
        self._active_priority: AudioPriority | None = None
        self._lock = asyncio.Lock()
        self._on_background_resume: Callable[[], Awaitable[None]] | None = None
        self._on_background_pause: Callable[[], Awaitable[None]] | None = None

        # 为每个优先级创建独立的播放器
        for p in AudioPriority:
            player = MpvPlayer(name=p.name.lower(), backend=backend, pulse_sink=pulse_sink)
            self.channels[p] = AudioChannel(priority=p, player=player)

    def set_background_callbacks(
        self,
        on_pause: Callable[[], Awaitable[None]] | None = None,
        on_resume: Callable[[], Awaitable[None]] | None = None,
    ):
        """设置背景音乐暂停/恢复回调"""
        self._on_background_pause = on_pause
        self._on_background_resume = on_resume

    async def play(self, priority: AudioPriority, url: str):
        """在指定优先级通道播放音频"""
        async with self._lock:
            channel = self.channels[priority]

            # 暂停所有低优先级通道
            for p in AudioPriority:
                if p < priority:
                    lower = self.channels[p]
                    if lower.player.state == PlayerState.PLAYING:
                        lower.was_playing = True
                        await lower.player.pause()
                        logger.info(f"暂停 {p.name} 通道 (被 {priority.name} 打断)")
                        if p == AudioPriority.BACKGROUND and self._on_background_pause:
                            await self._on_background_pause()

            await channel.player.play(url)
            self._active_priority = priority

    async def stop(self, priority: AudioPriority):
        """停止指定优先级通道，并恢复被打断的低优先级"""
        async with self._lock:
            channel = self.channels[priority]
            await channel.player.stop()

            if self._active_priority == priority:
                self._active_priority = None

            # 从高到低查找需要恢复的通道
            for p in sorted(AudioPriority, reverse=True):
                if p >= priority:
                    continue
                lower = self.channels[p]
                if lower.was_playing:
                    lower.was_playing = False
                    await lower.player.resume()
                    self._active_priority = p
                    logger.info(f"恢复 {p.name} 通道")
                    if p == AudioPriority.BACKGROUND and self._on_background_resume:
                        await self._on_background_resume()
                    break

    async def pause(self, priority: AudioPriority):
        """暂停指定优先级通道"""
        channel = self.channels[priority]
        await channel.player.pause()

    async def resume(self, priority: AudioPriority):
        """恢复指定优先级通道"""
        async with self._lock:
            channel = self.channels[priority]
            # 只有没有更高优先级在播放时才恢复
            for p in AudioPriority:
                if p > priority and self.channels[p].player.state == PlayerState.PLAYING:
                    channel.was_playing = True
                    logger.info(f"无法恢复 {priority.name}: {p.name} 正在播放")
                    return
            await channel.player.resume()

    async def set_volume(self, priority: AudioPriority, volume: int):
        """设置指定通道音量"""
        await self.channels[priority].player.set_volume(volume)

    def get_status(self) -> dict:
        """获取所有通道状态"""
        return {
            p.name: {
                "state": self.channels[p].player.state.name,
                "url": self.channels[p].player.current_url,
                "was_playing": self.channels[p].was_playing,
            }
            for p in AudioPriority
        }

    @property
    def active_priority(self) -> AudioPriority | None:
        return self._active_priority

    async def shutdown(self):
        """关闭所有播放器"""
        for channel in self.channels.values():
            await channel.player.shutdown()
        logger.info("AudioManager 已关闭")
