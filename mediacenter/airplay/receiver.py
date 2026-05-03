"""AirPlay 接收器 - 集成 shairport-sync

通过监控 shairport-sync 的 stderr 来检测播放状态，
自动与 AudioManager 的 BACKGROUND 优先级联动，
并通过 D-Bus MPRIS 接口对外暴露 pause/resume 控制。
"""

import asyncio
import logging
import os
import re
import signal
import tempfile
from collections import deque

logger = logging.getLogger(__name__)

SHAIRPORT_SYNC_BIN = "shairport-sync"
MPRIS_BUS_NAME = "org.mpris.MediaPlayer2.ShairportSync"
MPRIS_OBJECT = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"


class AirPlayReceiver:
    """AirPlay 接收器

    封装 shairport-sync 的启动/停止，监听播放状态变化。
    - 开始播放：暂停 DLNA（via peer）和 BACKGROUND
    - 停止播放：恢复 DLNA 和 BACKGROUND
    - 支持被外部（DLNA、TTS）通过 pause()/resume() 打断
    """

    def __init__(self, audio_manager, config: dict):
        self.manager = audio_manager
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._monitor_task: asyncio.Task | None = None
        self._is_playing = False
        self._metadata_pipe = "/tmp/shairport-sync-metadata"
        # peer 由 MediaCenter 在启动后注入
        self._dlna = None
        # 标记是否由本接收器主动暂停了 MPD，用于停止时恢复
        self._paused_mpd = False
        self._active_config_path: str | None = None
        self._runtime_config_path: str | None = None
        self._last_exit_code: int | None = None
        self._last_error: str | None = None
        self._recent_output: deque[str] = deque(maxlen=20)
        self._volume_apply_task: asyncio.Task | None = None

    def _resolve_name(self) -> str:
        return os.environ.get(
            "AIRPLAY_NAME",
            self.config.get("name", "OpenWrt MediaCenter"),
        )

    def _resolve_config_path(self) -> str | None:
        configured = self.config.get("config_path")
        if configured:
            return configured if os.path.exists(configured) else None

        docker_override = "/etc/mediacenter/shairport-sync.conf"
        if os.path.exists(docker_override):
            return docker_override
        return None

    def _default_volume(self) -> int:
        return max(0, min(100, int(self.config.get("default_volume", 80))))

    def _write_runtime_config(self) -> str | None:
        backend = getattr(self.manager, "backend", "pulse")
        pulse_sink = getattr(self.manager, "pulse_sink", "")
        if backend == "pulse":
            output_backend = "pa"
        elif backend == "alsa":
            output_backend = "alsa"
        else:
            return None

        path = os.path.join(tempfile.gettempdir(), "mediacenter-shairport-sync.conf")
        lines = [
            "general = {",
            '    interpolation = "basic";',
            f'    output_backend = "{output_backend}";',
            "    drift_tolerance_in_seconds = 0.002;",
            "    resync_threshold_in_seconds = 0.050;",
            "};",
            "",
            "metadata = {",
            '    enabled = "yes";',
            '    include_cover_art = "no";',
            f'    pipe_name = "{self._metadata_pipe}";',
            "    pipe_timeout = 5000;",
            "};",
            "",
            "sessioncontrol = {",
            "    session_timeout = 120;",
            "};",
            "",
            "diagnostics = {",
            "    log_verbosity = 1;",
            "};",
        ]

        if output_backend == "pa":
            lines.extend([
                "",
                "pa = {",
                '    application_name = "shairport-sync";',
            ])
            if pulse_sink:
                escaped_sink = pulse_sink.replace('"', '\\"')
                lines.append(f'    sink = "{escaped_sink}";')
            lines.append("};")

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        self._runtime_config_path = path
        return path

    def _process_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def _build_command(
        self,
        name: str,
        port: int,
        config_path: str | None,
        include_identity_args: bool = False,
    ) -> list[str]:
        if config_path and os.path.exists(config_path):
            cmd = [SHAIRPORT_SYNC_BIN, "-c", config_path]
            if include_identity_args:
                cmd.extend(["-a", name, "-p", str(port)])
            return cmd

        cmd = [
            SHAIRPORT_SYNC_BIN,
            "-a", name,
            "-p", str(port),
        ]

        backend = getattr(self.manager, "backend", "pulse")
        if backend == "pulse":
            cmd.extend(["-o", "pa"])
        elif backend == "alsa":
            cmd.extend(["-o", "alsa"])

        cmd.extend([
            "--metadata-pipename", self._metadata_pipe,
            "-v",
        ])
        return cmd

    def _format_recent_output(self) -> str:
        return " | ".join(self._recent_output)

    @staticmethod
    def _parse_shairport_sink_input_id(output: str) -> int | None:
        current_id: int | None = None
        current_lines: list[str] = []

        for raw_line in output.splitlines():
            line = raw_line.strip()
            match = re.match(r"Sink Input #(\d+)", line)
            if match:
                if current_id is not None and "shairport" in "\n".join(current_lines).lower():
                    return current_id
                current_id = int(match.group(1))
                current_lines = []
                continue
            if current_id is not None:
                current_lines.append(line)

        if current_id is not None and "shairport" in "\n".join(current_lines).lower():
            return current_id
        return None

    async def _find_shairport_sink_input_id(self) -> int | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pactl",
                "list",
                "sink-inputs",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.debug("pactl 不存在，无法为 AirPlay 自动设置默认音量")
            return None

        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.debug(
                "查询 Pulse sink-input 失败: %s",
                stderr.decode(errors="ignore").strip() or proc.returncode,
            )
            return None

        return self._parse_shairport_sink_input_id(stdout.decode(errors="ignore"))

    async def _apply_default_volume(self):
        if getattr(self.manager, "backend", "pulse") != "pulse":
            return

        target_volume = self._default_volume()
        for attempt in range(10):
            sink_input_id = await self._find_shairport_sink_input_id()
            if sink_input_id is None:
                await asyncio.sleep(0.3)
                continue

            try:
                proc = await asyncio.create_subprocess_exec(
                    "pactl",
                    "set-sink-input-volume",
                    str(sink_input_id),
                    f"{target_volume}%",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                logger.debug("pactl 不存在，无法为 AirPlay 自动设置默认音量")
                return

            _, stderr = await proc.communicate()
            if proc.returncode == 0:
                logger.info(
                    "AirPlay 默认音量已设置为 %s%% (sink-input=%s)",
                    target_volume,
                    sink_input_id,
                )
                return

            logger.debug(
                "设置 AirPlay 默认音量失败 (attempt=%s): %s",
                attempt + 1,
                stderr.decode(errors="ignore").strip() or proc.returncode,
            )
            await asyncio.sleep(0.3)

        logger.warning("AirPlay 默认音量设置失败，未能定位 shairport-sync 的 Pulse sink-input")

    async def _spawn_process(self, cmd: list[str], config_path: str | None):
        self._recent_output.clear()
        self._last_error = None
        self._last_exit_code = None
        self._active_config_path = config_path
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._monitor_task = asyncio.create_task(self._monitor_output())

    async def _wait_for_stable_startup(self, config_path: str | None) -> bool:
        if not self._process:
            return False

        try:
            await asyncio.wait_for(self._process.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            return True

        if self._monitor_task:
            try:
                await asyncio.wait_for(asyncio.shield(self._monitor_task), timeout=1.0)
            except asyncio.TimeoutError:
                pass

        exit_code = self._process.returncode
        self._last_exit_code = exit_code
        recent_output = self._format_recent_output()
        if not self._last_error:
            self._last_error = recent_output or f"shairport-sync exited with code {exit_code}"

        logger.error(
            "shairport-sync 启动后立即退出: exit=%s, config=%s, output=%s",
            exit_code,
            config_path or "builtin",
            recent_output or "（无输出）",
        )
        self._process = None
        self._monitor_task = None
        self._is_playing = False
        return False

    def _state(self) -> str:
        if not self.config.get("enabled", True):
            return "disabled"
        if not self._process_running():
            return "stopped"
        if self._is_playing:
            return "playing"
        return "idle"

    def _status_text(self) -> str:
        state = self._state()
        if state == "disabled":
            return "已禁用"
        if state == "playing":
            return f"{self._resolve_name()} 串流中"
        if state == "idle":
            return f"{self._resolve_name()} 待机中"
        return "未运行"

    async def start(self):
        if not self.config.get("enabled", True):
            logger.info("AirPlay 未启用")
            return

        if self._process_running():
            logger.info("AirPlay 已在运行，跳过重复启动")
            return

        name = self._resolve_name()
        port = self.config.get("port", 5000)
        config_path = self._resolve_config_path()
        generated_runtime_config = False
        if not config_path:
            config_path = self._write_runtime_config()
            generated_runtime_config = bool(config_path)

        try:
            cmd = self._build_command(
                name,
                port,
                config_path,
                include_identity_args=generated_runtime_config,
            )
            await self._spawn_process(cmd, config_path)
            logger.info(
                f"shairport-sync 启动中: {name} (port {port}), "
                f"PID={self._process.pid}, config={config_path or 'builtin'}"
            )

            if await self._wait_for_stable_startup(config_path):
                logger.info("shairport-sync 已进入稳定运行状态")
                return

            if config_path:
                logger.warning(
                    "AirPlay 配置文件 %s 启动失败，改用内置参数重试",
                    config_path,
                )
                cmd = self._build_command(name, port, None)
                await self._spawn_process(cmd, None)
                logger.info(
                    f"shairport-sync 回退启动中: {name} (port {port}), "
                    f"PID={self._process.pid}, config=builtin"
                )
                if await self._wait_for_stable_startup(None):
                    logger.info("shairport-sync 已通过内置参数稳定运行")
                    return
        except FileNotFoundError:
            logger.error("shairport-sync 未安装! 请运行: opkg install shairport-sync")

    async def pause(self):
        """通过 D-Bus MPRIS 暂停 shairport-sync（由 DLNA 或 TTS 调用）"""
        try:
            from dbus_next.aio import MessageBus
            from dbus_next import BusType
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            intr = await bus.introspect(MPRIS_BUS_NAME, MPRIS_OBJECT)
            iface = bus.get_proxy_object(MPRIS_BUS_NAME, MPRIS_OBJECT, intr) \
                       .get_interface(MPRIS_PLAYER_IFACE)
            await iface.call_pause()
            bus.disconnect()
        except Exception as e:
            logger.warning(f"AirPlay 暂停失败: {e}")

    async def resume(self):
        """通过 D-Bus MPRIS 恢复 shairport-sync（由 DLNA 或 TTS 调用）"""
        try:
            from dbus_next.aio import MessageBus
            from dbus_next import BusType
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            intr = await bus.introspect(MPRIS_BUS_NAME, MPRIS_OBJECT)
            iface = bus.get_proxy_object(MPRIS_BUS_NAME, MPRIS_OBJECT, intr) \
                       .get_interface(MPRIS_PLAYER_IFACE)
            await iface.call_play()
            bus.disconnect()
        except Exception as e:
            logger.warning(f"AirPlay 恢复失败: {e}")

    async def _monitor_output(self):
        if not self._process or not self._process.stdout:
            return
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="ignore").strip()
                if not text:
                    continue

                self._recent_output.append(text)
                if "Play" in text and "Begin" in text:
                    await self._on_play_start()
                elif "Play" in text and "End" in text:
                    await self._on_play_end()
                elif "Connection" in text and "from" in text:
                    logger.info(f"AirPlay 设备已连接: {text}")
                else:
                    lowered = text.lower()
                    if any(token in lowered for token in ("error", "fatal", "failed", "unable", "warn")):
                        logger.warning(f"AirPlay[shairport-sync]: {text}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"AirPlay 监控异常: {e}")
        finally:
            if self._process and self._process.returncode is not None:
                self._last_exit_code = self._process.returncode
                if self._process.returncode != 0 and not self._last_error:
                    self._last_error = (
                        self._format_recent_output()
                        or f"shairport-sync exited with code {self._process.returncode}"
                    )

    async def _on_play_start(self):
        if self._is_playing:
            return
        self._is_playing = True
        logger.info("AirPlay 开始播放")

        if self._volume_apply_task and not self._volume_apply_task.done():
            self._volume_apply_task.cancel()
        self._volume_apply_task = asyncio.create_task(self._apply_default_volume())

        # 打断 DLNA（平级优先级，后到打断先到）
        if self._dlna and self._dlna.is_streaming:
            await self._dlna.pause()
            self._paused_mpd = True

        # 暂停背景音乐（若 DLNA 已把它暂停，player.is_playing 为 False，幂等）
        from mediacenter.audio.manager import AudioPriority
        ch = self.manager.channels[AudioPriority.BACKGROUND]
        if ch.player.is_playing:
            ch.was_playing = True
            await ch.player.pause()

    async def _on_play_end(self):
        if not self._is_playing:
            return
        self._is_playing = False
        logger.info("AirPlay 停止播放")

        # 恢复被我们暂停的 DLNA
        if self._paused_mpd and self._dlna:
            self._paused_mpd = False
            await self._dlna.resume()

        # 只在 DLNA 也不在串流时才恢复背景音乐
        if not (self._dlna and self._dlna.is_streaming):
            from mediacenter.audio.manager import AudioPriority
            ch = self.manager.channels[AudioPriority.BACKGROUND]
            if ch.was_playing:
                ch.was_playing = False
                await ch.player.resume()

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    def get_status(self) -> dict:
        managed_process_running = self._process_running()
        state = self._state()
        return {
            "enabled": self.config.get("enabled", True),
            "running": managed_process_running,
            "available": managed_process_running,
            "managed_process_running": managed_process_running,
            "managed_by": "mediacenter",
            "is_playing": self._is_playing,
            "state": state,
            "status_text": self._status_text(),
            "name": self._resolve_name(),
            "config_path": self._active_config_path,
            "last_exit_code": self._last_exit_code,
            "last_error": self._last_error,
        }

    async def stop(self):
        if self._volume_apply_task:
            self._volume_apply_task.cancel()
            try:
                await self._volume_apply_task
            except asyncio.CancelledError:
                pass

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

        self._is_playing = False
        self._paused_mpd = False
        self._monitor_task = None
        self._volume_apply_task = None
        self._process = None

        if self._runtime_config_path and os.path.exists(self._runtime_config_path):
            try:
                os.unlink(self._runtime_config_path)
            except OSError:
                pass
        self._runtime_config_path = None

        logger.info("AirPlay 服务已停止")
