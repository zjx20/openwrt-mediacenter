"""AirPlay 接收器 - 集成 shairport-sync

通过监控 shairport-sync 的元数据管道来检测 AirPlay 播放状态，
自动触发音频管理器的优先级切换。
"""

import asyncio
import logging
import os
import signal

logger = logging.getLogger(__name__)

SHAIRPORT_SYNC_BIN = "shairport-sync"


class AirPlayReceiver:
    """AirPlay 接收器

    封装 shairport-sync 的启动/停止，监听播放状态变化，
    自动与 AudioManager 的 STREAM 优先级联动。
    """

    def __init__(self, audio_manager, config: dict):
        self.manager = audio_manager
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._monitor_task: asyncio.Task | None = None
        self._is_playing = False
        self._metadata_pipe = "/tmp/shairport-sync-metadata"

    async def start(self):
        """启动 shairport-sync 服务"""
        if not self.config.get("enabled", True):
            logger.info("AirPlay 未启用")
            return

        name = self.config.get("name", "OpenWrt MediaCenter")
        port = self.config.get("port", 5000)

        cmd = [
            SHAIRPORT_SYNC_BIN,
            "-a", name,
            "-p", str(port),
            "--metadata-pipename", self._metadata_pipe,
            "-v",  # verbose, 用于检测播放状态
        ]

        config_path = self.config.get("config_path")
        if config_path and os.path.exists(config_path):
            cmd = [SHAIRPORT_SYNC_BIN, "-c", config_path]

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info(
                f"shairport-sync 已启动: {name} (port {port}), "
                f"PID={self._process.pid}"
            )
            self._monitor_task = asyncio.create_task(self._monitor_output())
        except FileNotFoundError:
            logger.error(
                "shairport-sync 未安装! "
                "请运行: opkg install shairport-sync"
            )

    async def _monitor_output(self):
        """监控 shairport-sync 的输出来检测播放状态"""
        if not self._process or not self._process.stderr:
            return

        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="ignore").strip()

                if "Play" in text and "Begin" in text:
                    await self._on_play_start()
                elif "Play" in text and "End" in text:
                    await self._on_play_end()
                elif "Connection" in text and "from" in text:
                    logger.info(f"AirPlay 设备已连接: {text}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"AirPlay 监控异常: {e}")

    async def _on_play_start(self):
        """AirPlay 开始播放"""
        if not self._is_playing:
            self._is_playing = True
            logger.info("AirPlay 开始播放")
            # shairport-sync 自己控制音频输出，
            # 我们只需要通知 manager 暂停低优先级
            from mediacenter.audio.manager import AudioPriority
            for p in [AudioPriority.BACKGROUND]:
                ch = self.manager.channels[p]
                if ch.player.is_playing:
                    ch.was_playing = True
                    await ch.player.pause()

    async def _on_play_end(self):
        """AirPlay 停止播放"""
        if self._is_playing:
            self._is_playing = False
            logger.info("AirPlay 停止播放")
            from mediacenter.audio.manager import AudioPriority
            for p in sorted(
                [AudioPriority.BACKGROUND], reverse=True
            ):
                ch = self.manager.channels[p]
                if ch.was_playing:
                    ch.was_playing = False
                    await ch.player.resume()

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    def get_status(self) -> dict:
        return {
            "enabled": self.config.get("enabled", True),
            "running": self._process is not None
            and self._process.returncode is None,
            "is_playing": self._is_playing,
            "name": self.config.get("name", "OpenWrt MediaCenter"),
        }

    async def stop(self):
        """停止 shairport-sync"""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        if self._process and self._process.returncode is None:
            try:
                self._process.send_signal(signal.SIGTERM)
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass

        logger.info("AirPlay 服务已停止")
