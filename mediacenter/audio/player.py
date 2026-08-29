"""基于 mpv 的音频播放器"""

import asyncio
import json
import logging
import os
import shlex
import signal
import tempfile
import time
from collections import deque
from enum import IntEnum
from pathlib import Path
from typing import Callable

from .pulse import env_with_media_role

logger = logging.getLogger(__name__)


class PlayerState(IntEnum):
    STOPPED = 0
    PLAYING = 1
    PAUSED = 2


class MpvPlayer:
    """通过 mpv 的 JSON IPC 协议控制音频播放。

    每个 MpvPlayer 实例管理一个 mpv 子进程，通过 Unix socket 通信。
    """

    def __init__(self, name: str = "default", backend: str = "pulse", pulse_sink: str = "", pulse_media_role: str = ""):
        self.name = name
        self.backend = backend
        self.pulse_sink = pulse_sink
        self.pulse_media_role = pulse_media_role
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
        self._stderr_task: asyncio.Task | None = None
        self._current_url: str | None = None
        self._volume: int = 50
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._last_start_cmd: list[str] = []
        self._last_start_duration_ms: int | None = None
        self._last_start_error: str | None = None
        self._last_pulse_prop: str | None = None

    async def _capture_stderr(self):
        """持续采集 mpv stderr，便于启动失败时诊断。"""
        process = self._process
        if process is None or process.stderr is None:
            return

        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if text:
                    self._stderr_tail.append(text)
                    logger.debug(f"[{self.name}] mpv stderr: {text}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[{self.name}] 读取 mpv stderr 失败: {e}")

    def _format_start_failure(self, reason: str) -> str:
        """格式化 mpv 启动失败诊断信息。"""
        stderr_tail = " | ".join(self._stderr_tail) if self._stderr_tail else "<empty>"
        cmd = shlex.join(self._last_start_cmd) if self._last_start_cmd else "<unknown>"
        pid = self._process.pid if self._process else None
        returncode = self._process.returncode if self._process else None
        return (
            f"{reason}; cmd={cmd}; backend={self.backend}; "
            f"pulse_sink={self.pulse_sink or '-'}; media_role={self.pulse_media_role or '-'}; "
            f"pulse_prop={self._last_pulse_prop or '-'}; "
            f"socket={self._socket_path}; pid={pid}; returncode={returncode}; "
            f"startup_ms={self._last_start_duration_ms}; stderr_tail={stderr_tail}"
        )

    async def _reset_runtime(self):
        """清理 IPC 连接和 mpv 子进程状态。"""
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None

        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None

        self._reader = None

        if self._process and self._process.returncode is None:
            try:
                self._process.send_signal(signal.SIGTERM)
                await asyncio.wait_for(self._process.wait(), timeout=3)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._process.kill()
                    await self._process.wait()
                except ProcessLookupError:
                    pass
        elif self._process:
            try:
                await asyncio.wait_for(self._process.wait(), timeout=0.2)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass

        if self._stderr_task:
            if not self._stderr_task.done():
                self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._stderr_task = None

        self._process = None

        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)

        self.state = PlayerState.STOPPED

    async def _ensure_mpv(self):
        """确保 mpv 进程在运行"""
        if (
            self._process is not None
            and self._process.returncode is None
            and self._reader is not None
            and self._writer is not None
        ):
            return

        if self._process is not None:
            logger.warning(
                f"[{self.name}] 检测到残留 mpv 状态，准备重建: "
                f"returncode={self._process.returncode}, socket={self._socket_path}"
            )
            await self._reset_runtime()

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
        env = os.environ.copy()
        self._last_pulse_prop = None
        if self.backend == "pulse":
            cmd.append("--ao=pulse")
            if self.pulse_sink:
                cmd.append(f"--pulse-sink={self.pulse_sink}")
            if self.pulse_media_role:
                env = env_with_media_role(env, self.pulse_media_role)
                self._last_pulse_prop = env["PULSE_PROP"]
        elif self.backend == "alsa":
            cmd.append("--ao=alsa")

        self._stderr_tail.clear()
        self._last_start_cmd = cmd[:]
        self._last_start_duration_ms = None
        self._last_start_error = None
        start_at = time.monotonic()

        logger.info(
            f"[{self.name}] 启动 mpv: cmd={shlex.join(cmd)} backend={self.backend} "
            f"pulse_sink={self.pulse_sink or '-'} media_role={self.pulse_media_role or '-'} "
            f"pulse_prop={self._last_pulse_prop or '-'} socket={self._socket_path}"
        )

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as e:
            self._last_start_duration_ms = int((time.monotonic() - start_at) * 1000)
            self._last_start_error = self._format_start_failure(
                f"failed to spawn mpv process: {e}"
            )
            logger.error(f"[{self.name}] {self._last_start_error}")
            raise RuntimeError(self._last_start_error) from e

        self._stderr_task = asyncio.create_task(self._capture_stderr())

        # 等待 socket 就绪
        socket_ready = False
        for _ in range(50):
            if os.path.exists(self._socket_path):
                socket_ready = True
                break
            if self._process.returncode is not None:
                break
            await asyncio.sleep(0.1)
        self._last_start_duration_ms = int((time.monotonic() - start_at) * 1000)

        if not socket_ready:
            self._last_start_error = self._format_start_failure(
                "mpv IPC socket not created"
            )
            logger.error(f"[{self.name}] {self._last_start_error}")
            await self._reset_runtime()
            raise RuntimeError(self._last_start_error)

        connect_error: OSError | None = None
        for _ in range(20):
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(
                    self._socket_path
                )
                connect_error = None
                break
            except OSError as e:
                connect_error = e
                await asyncio.sleep(0.1)

        if connect_error is not None:
            self._last_start_duration_ms = int((time.monotonic() - start_at) * 1000)
            self._last_start_error = self._format_start_failure(
                f"mpv IPC socket connect failed: {connect_error}"
            )
            logger.error(f"[{self.name}] {self._last_start_error}")
            await self._reset_runtime()
            raise RuntimeError(self._last_start_error) from connect_error

        self._listen_task = asyncio.create_task(self._listen())
        logger.info(
            f"[{self.name}] mpv 进程已启动, PID={self._process.pid}, "
            f"socket={self._socket_path}, startup_ms={self._last_start_duration_ms}"
        )

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

    async def _command(self, *args, timeout: float = 5.0) -> dict:
        """发送 mpv IPC 命令"""
        await self._ensure_mpv()
        self._request_id += 1
        rid = self._request_id
        cmd = {"command": list(args), "request_id": rid}

        future = asyncio.get_event_loop().create_future()
        self._response_futures[rid] = future

        try:
            if self._writer is None:
                raise RuntimeError("mpv IPC writer is not ready")
            self._writer.write((json.dumps(cmd) + "\n").encode())
            await self._writer.drain()
            resp = await asyncio.wait_for(future, timeout=timeout)
        except (asyncio.TimeoutError, ConnectionError, BrokenPipeError, RuntimeError) as e:
            self._response_futures.pop(rid, None)
            self._last_start_error = f"mpv 命令失败: command={list(args)!r}, error={e}"
            logger.warning(f"[{self.name}] {self._last_start_error}")
            raise RuntimeError(self._last_start_error) from e

        if resp.get("error") not in (None, "success"):
            self._last_start_error = (
                f"mpv 命令返回错误: command={list(args)!r}, error={resp.get('error')}"
            )
            logger.warning(f"[{self.name}] {self._last_start_error}")
            raise RuntimeError(self._last_start_error)

        self._last_start_error = None
        return resp

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
        if self._process is None or self._process.returncode is not None or self._writer is None:
            self.state = PlayerState.STOPPED
            self._current_url = None
            return
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

    def get_diagnostics(self) -> dict:
        """返回播放器诊断信息。"""
        return {
            "name": self.name,
            "backend": self.backend,
            "pulse_sink": self.pulse_sink,
            "pulse_media_role": self.pulse_media_role,
            "pulse_prop": self._last_pulse_prop,
            "socket_path": self._socket_path,
            "process_pid": self._process.pid if self._process else None,
            "process_returncode": self._process.returncode if self._process else None,
            "last_start_cmd": self._last_start_cmd,
            "last_start_duration_ms": self._last_start_duration_ms,
            "last_start_error": self._last_start_error,
            "stderr_tail": list(self._stderr_tail),
        }

    async def wait_for_end(self):
        """等待当前播放结束"""
        while self.state == PlayerState.PLAYING:
            await asyncio.sleep(0.5)

    async def shutdown(self):
        """关闭 mpv 进程"""
        await self._reset_runtime()
        logger.info(f"[{self.name}] mpv 已关闭")

    @property
    def volume(self) -> int:
        return self._volume

    @property
    def is_playing(self) -> bool:
        return self.state == PlayerState.PLAYING

    @property
    def is_paused(self) -> bool:
        return self.state == PlayerState.PAUSED

    @property
    def current_url(self) -> str | None:
        return self._current_url
