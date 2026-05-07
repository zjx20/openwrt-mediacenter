"""音频通道管理器

为不同音频用途分配独立的 mpv 通道（每个通道带专属的 PulseAudio media.role）：
  - BACKGROUND: 背景音乐，role=background
  - STREAM: 预留通道（暂未使用），role=background
  - TTS: TTS 播报，role=tts

不再做"高优先级抢占低优先级"的逻辑暂停 —— 各通道之间的音量协调
完全交给 PulseAudio module-role-ducking：当 role=tts 的流出现时，
其它角色的音量会被系统自动压低，TTS 结束后自动恢复。
"""

import asyncio
import logging
from enum import IntEnum

from .player import MpvPlayer

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
        self.available = True
        self.last_error: str | None = None


class AudioManager:
    """多优先级音频管理器"""

    def __init__(self, backend: str = "pulse", pulse_sink: str = ""):
        self.backend = backend
        self.pulse_sink = pulse_sink
        self.channels: dict[AudioPriority, AudioChannel] = {}
        self._active_priority: AudioPriority | None = None
        self._lock = asyncio.Lock()

        # 为每个优先级创建独立的播放器
        _pa_roles = {AudioPriority.TTS: "tts", AudioPriority.BACKGROUND: "background", AudioPriority.STREAM: "background"}
        for p in AudioPriority:
            player = MpvPlayer(
                name=p.name.lower(),
                backend=backend,
                pulse_sink=pulse_sink,
                pulse_media_role=_pa_roles.get(p, ""),
            )
            self.channels[p] = AudioChannel(priority=p, player=player)

    async def _call_channel(self, priority: AudioPriority, operation: str, action):
        """执行单通道操作，并在失败时记录错误状态。"""
        channel = self.channels[priority]
        try:
            result = await action(channel.player)
        except Exception as e:
            channel.available = False
            channel.last_error = str(e)
            logger.warning(
                f"音频通道操作失败: channel={priority.name.lower()} "
                f"operation={operation} error={e}",
                exc_info=True,
            )
            raise

        diagnostics = channel.player.get_diagnostics()
        channel.last_error = diagnostics.get("last_start_error")
        channel.available = channel.last_error is None
        return result

    async def play(self, priority: AudioPriority, url: str):
        """在指定通道播放音频。

        多通道并行：不再暂停其它通道，PulseAudio 会按 media.role 自动 ducking。
        """
        async with self._lock:
            await self._call_channel(
                priority,
                "play",
                lambda player: player.play(url),
            )
            self._active_priority = priority

    async def stop(self, priority: AudioPriority):
        """停止指定通道。"""
        async with self._lock:
            try:
                await self._call_channel(
                    priority,
                    "stop",
                    lambda player: player.stop(),
                )
            finally:
                if self._active_priority == priority:
                    self._active_priority = None

    async def pause(self, priority: AudioPriority):
        """暂停指定通道"""
        await self._call_channel(
            priority,
            "pause",
            lambda player: player.pause(),
        )

    async def resume(self, priority: AudioPriority):
        """恢复指定通道"""
        await self._call_channel(
            priority,
            "resume",
            lambda player: player.resume(),
        )

    async def set_volume(self, priority: AudioPriority, volume: int):
        """设置指定通道音量"""
        await self._call_channel(
            priority,
            "set_volume",
            lambda player: player.set_volume(volume),
        )

    def get_status(self) -> dict:
        """获取所有通道状态"""
        return {
            p.name: {
                "state": self.channels[p].player.state.name,
                "url": self.channels[p].player.current_url,
                "volume": self.channels[p].player.volume,
                "was_playing": self.channels[p].was_playing,
                "available": self.channels[p].available,
                "last_error": self.channels[p].last_error,
                "diagnostics": self.channels[p].player.get_diagnostics(),
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
