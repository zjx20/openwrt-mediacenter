"""DLNA 渲染器 - 集成 gmrender-resurrect

通过运行 gmrender-resurrect 提供 DLNA 渲染服务，
设备可以通过 DLNA 协议推送音频到本设备播放。
"""

import asyncio
import logging
import os
import signal

logger = logging.getLogger(__name__)

GMRENDER_BIN = "gmediarender"


class DLNARenderer:
    """DLNA 渲染器

    封装 gmrender-resurrect (gmediarender) 的管理。
    """

    def __init__(self, audio_manager, config: dict):
        self.manager = audio_manager
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._monitor_task: asyncio.Task | None = None
        self._is_playing = False

    async def start(self):
        """启动 gmediarender 服务"""
        if not self.config.get("enabled", True):
            logger.info("DLNA 未启用")
            return

        name = self.config.get("name", "OpenWrt MediaCenter")
        port = self.config.get("port", 49152)

        cmd = [
            GMRENDER_BIN,
            "-f", name,
            "-p", str(port),
            "--gstout-audiosink=pulsesink",
            "--logfile", "/tmp/gmrender.log",
        ]

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info(
                f"gmediarender 已启动: {name} (port {port}), "
                f"PID={self._process.pid}"
            )
            self._monitor_task = asyncio.create_task(self._monitor_output())
        except FileNotFoundError:
            logger.error(
                "gmediarender 未安装! "
                "请运行: opkg install gmrender-resurrect"
            )

    async def _monitor_output(self):
        """监控 gmediarender 输出"""
        if not self._process:
            return

        async def _drain(stream, level):
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode(errors="ignore").strip()
                if text:
                    logger.log(level, f"DLNA: {text}")

        try:
            await asyncio.gather(
                _drain(self._process.stdout, logging.DEBUG),
                _drain(self._process.stderr, logging.WARNING),
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"DLNA 监控异常: {e}")

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
        """停止 gmediarender"""
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

        logger.info("DLNA 服务已停止")
