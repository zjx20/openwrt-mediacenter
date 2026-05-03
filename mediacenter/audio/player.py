"""基于 mpv 的音频播放器"""

import asyncio
import json
import logging
import os
import signal
import tempfile
from enum import IntEnum
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


class PlayerState(IntEnum):
    STOPPED = 0
    PLAYING = 1
    PAUSED = 2


class MpvPlayer:
    """通过 mpv 的 JSON IPC 协议控制音频播放。

    每个 MpvPlayer 实例管理一个 mpv 子进程，通过 Unix socket 通信。
    """

    def __init__(self, name: str = "default", backend: str = "pulse", pulse_sink: str = ""):
        self.name = name
        self.backend = backend
        self.pulse_sink = pulse_sink
        self.state = PlayerState.STOPPED
        self._process: asyncio.subprocess.Process | None = None
        self._socket_path = os.path.join(
            tempfile.gettempdir(), f"mpv-mediacenter-{name}.sock"
        )
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 0
        self._response_futures: dict[int, asyncio.Future] = {}
        self._event_handlers: dict[str, list[Callable]] = {}
        self._listen_task: asyncio.Task | None = None
        self._current_url: str | None = None
        self._volume: int = 50

    async def _ensure_mpv(self):
        """确保 mpv 进程在运行"""
        if self._process is not None and self._process.returncode is None:
            return

        # 清理旧的 socket
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)

        cmd = [
            "mpv",
            "--idle=yes",
            "--no-video",
            "--no-terminal",
            f"--input-ipc-server={self._socket_path}",
            f"--volume={self._volume}",
        ]
        if self.backend == "pulse":
            cmd.append("--ao=pulse")
            if self.pulse_sink:
                cmd.append(f"--pulse-sink={self.pulse_sink}")
        elif self.backend == "alsa":
            cmd.append("--ao=alsa")

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # 等待 socket 就绪
        for _ in range(50):
            if os.path.exists(self._socket_path):
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError(f"mpv IPC socket not created: {self._socket_path}")

        self._reader, self._writer = await asyncio.open_unix_connection(
            self._socket_path
        )
        self._listen_task = asyncio.create_task(self._listen())
        logger.info(f"[{self.name}] mpv 进程已启动, PID={self._process.pid}")

    async def _listen(self):
        """监听 mpv 的 IPC 消息"""
        try:
            while self._reader and not self._reader.at_eof():
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode().strip())
                except json.JSONDecodeError:
                    continue

                if "request_id" in msg and msg["request_id"] in self._response_futures:
                    fut = self._response_futures.pop(msg["request_id"])
                    if not fut.done():
                        fut.set_result(msg)
                elif "event" in msg:
                    event_name = msg["event"]
                    if event_name == "end-file":
                        self.state = PlayerState.STOPPED
                    for handler in self._event_handlers.get(event_name, []):
                        try:
                            result = handler(msg)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception as e:
                            logger.error(f"事件处理器异常: {e}")
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.state = PlayerState.STOPPED

    async def _command(self, *args, timeout: float = 5.0) -> dict | None:
        """发送 mpv IPC 命令"""
        await self._ensure_mpv()
        self._request_id += 1
        rid = self._request_id
        cmd = {"command": list(args), "request_id": rid}

        future = asyncio.get_event_loop().create_future()
        self._response_futures[rid] = future

        try:
            self._writer.write((json.dumps(cmd) + "\n").encode())
            await self._writer.drain()
            return await asyncio.wait_for(future, timeout=timeout)
        except (asyncio.TimeoutError, ConnectionError) as e:
            self._response_futures.pop(rid, None)
            logger.warning(f"[{self.name}] mpv 命令超时或连接错误: {e}")
            return None

    def on_event(self, event_name: str, handler: Callable):
        """注册事件处理器"""
        self._event_handlers.setdefault(event_name, []).append(handler)

    async def play(self, url: str, wait: bool = False):
        """播放音频

        Args:
            url: 音频 URL 或本地文件路径
            wait: 是否等待播放完成
        """
        self._current_url = url
        await self._command("loadfile", url, "replace")
        self.state = PlayerState.PLAYING
        logger.info(f"[{self.name}] 开始播放: {url}")

        if wait:
            await self.wait_for_end()

    async def play_list(self, urls: list[str]):
        """播放列表"""
        if not urls:
            return
        await self.play(urls[0])
        for url in urls[1:]:
            await self._command("loadfile", url, "append")

    async def pause(self):
        """暂停"""
        if self.state == PlayerState.PLAYING:
            await self._command("set_property", "pause", True)
            self.state = PlayerState.PAUSED
            logger.info(f"[{self.name}] 已暂停")

    async def resume(self):
        """恢复"""
        if self.state == PlayerState.PAUSED:
            await self._command("set_property", "pause", False)
            self.state = PlayerState.PLAYING
            logger.info(f"[{self.name}] 已恢复")

    async def stop(self):
        """停止"""
        await self._command("stop")
        self.state = PlayerState.STOPPED
        self._current_url = None
        logger.info(f"[{self.name}] 已停止")

    async def set_volume(self, volume: int):
        """设置音量 (0-100)"""
        self._volume = max(0, min(100, volume))
        await self._command("set_property", "volume", self._volume)

    async def get_position(self) -> float | None:
        """获取当前播放位置 (秒)"""
        resp = await self._command("get_property", "time-pos")
        if resp and "data" in resp:
            return resp["data"]
        return None

    async def get_duration(self) -> float | None:
        """获取总时长 (秒)"""
        resp = await self._command("get_property", "duration")
        if resp and "data" in resp:
            return resp["data"]
        return None

    async def get_metadata(self) -> dict:
        """获取当前播放的媒体元数据"""
        resp = await self._command("get_property", "metadata")
        if resp and "data" in resp and isinstance(resp["data"], dict):
            return resp["data"]
        return {}

    async def wait_for_end(self):
        """等待当前播放结束"""
        while self.state == PlayerState.PLAYING:
            await asyncio.sleep(0.5)

    async def shutdown(self):
        """关闭 mpv 进程"""
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass

        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

        if self._process and self._process.returncode is None:
            try:
                self._process.send_signal(signal.SIGTERM)
                await asyncio.wait_for(self._process.wait(), timeout=3)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass

        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)

        self.state = PlayerState.STOPPED
        logger.info(f"[{self.name}] mpv 已关闭")

    @property
    def is_playing(self) -> bool:
        return self.state == PlayerState.PLAYING

    @property
    def is_paused(self) -> bool:
        return self.state == PlayerState.PAUSED

    @property
    def current_url(self) -> str | None:
        return self._current_url
