"""DLNA 渲染器 - upmpdcli + MPD

upmpdcli 实现 UPnP MediaRenderer / OpenHome 协议层，把 DLNA 控制端
的指令翻译成 MPD 命令；MPD 用 libavcodec/libavformat 解码音频，
通过 PulseAudio 输出。镜像里不再需要 GStreamer 栈。
"""

import asyncio
import logging
import signal
from pathlib import Path

from .proxy import DLNAProxy

logger = logging.getLogger(__name__)

MPD_BIN = "mpd"
UPMPDCLI_BIN = "upmpdcli"
MPD_HOST = "127.0.0.1"
MPD_PORT = 6600
RUNTIME_DIR = Path("/tmp/dlna")


class DLNARenderer:
    """DLNA 渲染器：管理 mpd + upmpdcli 一对常驻进程。

    _is_streaming: MPD 有内容加载中（play 或 pause），用于优先级判断
    _is_playing:   MPD 实际正在播放（play），用于 UI 状态
    """

    def __init__(self, audio_manager, config: dict):
        self.manager = audio_manager
        self.config = config
        self._mpd_process: asyncio.subprocess.Process | None = None
        self._upmpdcli_process: asyncio.subprocess.Process | None = None
        self._monitor_tasks: list[asyncio.Task] = []
        self._is_playing = False
        self._is_streaming = False
        # peer 由 MediaCenter 在启动后注入
        self._airplay = None
        # 标记是否由本渲染器主动暂停了 AirPlay，用于停止时恢复
        self._paused_airplay = False
        # URL 投屏代理：上游断连时自动 Range 续传，避免 ffmpeg "partial file"
        self._proxy = DLNAProxy()

    def _default_volume(self) -> int:
        return max(0, min(100, int(self.config.get("default_volume", 40))))

    async def start(self):
        if not self.config.get("enabled", True):
            logger.info("DLNA 未启用")
            return

        name = self.config.get("name", "OpenWrt MediaCenter")
        port = self.config.get("port", 49152)

        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        (RUNTIME_DIR / "music").mkdir(exist_ok=True)

        # 先起代理，MPD 启动时会立刻通过它连上游
        await self._proxy.start()

        mpd_conf = self._write_mpd_conf()
        upmpd_conf = self._write_upmpdcli_conf(name, port)

        if not await self._spawn_mpd(mpd_conf):
            return
        await self._wait_mpd_ready()
        await self._apply_default_volume("startup")
        await self._spawn_upmpdcli(upmpd_conf, name, port)

    async def _spawn_mpd(self, conf: Path) -> bool:
        try:
            self._mpd_process = await asyncio.create_subprocess_exec(
                MPD_BIN, "--stderr", "--no-daemon", str(conf),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("mpd 未安装，DLNA 无法启动")
            return False
        logger.info(f"mpd 已启动, PID={self._mpd_process.pid}")
        self._monitor_tasks.append(
            asyncio.create_task(self._monitor_output(self._mpd_process, "mpd"))
        )
        return True

    async def _spawn_upmpdcli(self, conf: Path, name: str, port: int):
        try:
            self._upmpdcli_process = await asyncio.create_subprocess_exec(
                UPMPDCLI_BIN, "-c", str(conf),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("upmpdcli 未安装，DLNA 无法启动")
            return
        logger.info(
            f"upmpdcli 已启动: {name} (upnpport={port}), "
            f"PID={self._upmpdcli_process.pid}"
        )
        self._monitor_tasks.append(
            asyncio.create_task(
                self._monitor_output(self._upmpdcli_process, "upmpdcli")
            )
        )
        self._monitor_tasks.append(
            asyncio.create_task(self._monitor_mpd_state())
        )

    def _write_mpd_conf(self) -> Path:
        path = RUNTIME_DIR / "mpd.conf"
        pulse_sink = getattr(self.manager, "pulse_sink", "")
        lines = [
            f'music_directory   "{RUNTIME_DIR}/music"',
            f'db_file           "{RUNTIME_DIR}/mpd.db"',
            f'state_file        "{RUNTIME_DIR}/mpd.state"',
            f'log_level         "info"',
            f'bind_to_address   "{MPD_HOST}"',
            f'port              "{MPD_PORT}"',
            f'auto_update       "no"',
            "",
            "input_cache {",
            '    size              "16 MB"',
            "}",
            "",
            "input {",
            '    plugin            "curl"',
            f'    proxy             "{self._proxy.url}"',
            '    connect_timeout   "30"',
            '    tcp_keepalive     "yes"',
            "}",
            "",
            "audio_output {",
            '    type          "pulse"',
            '    name          "PulseAudio"',
            '    mixer_type    "software"',
        ]
        if pulse_sink:
            lines.append(f'    sink          "{pulse_sink}"')
        lines.append("}")
        path.write_text("\n".join(lines) + "\n")
        return path

    def _write_upmpdcli_conf(self, name: str, port: int) -> Path:
        path = RUNTIME_DIR / "upmpdcli.conf"
        path.write_text(
            f'friendlyname = {name}\n'
            f'mpdhost = {MPD_HOST}\n'
            f'mpdport = {MPD_PORT}\n'
            f'upnpport = {port}\n'
            f'openhome = 0\n'
            f'loglevel = 3\n'
        )
        return path

    async def pause(self):
        """暂停 MPD 播放（由 AirPlay 或 TTS 调用）"""
        await self._mpd_command(b"pause 1")

    async def resume(self):
        """恢复 MPD 播放（由 AirPlay 或 TTS 调用）"""
        await self._mpd_command(b"pause 0")

    async def _mpd_command(self, cmd: bytes):
        """发送单次 MPD 控制命令（独立连接，不影响 idle 监控）"""
        try:
            reader, writer = await asyncio.open_connection(MPD_HOST, MPD_PORT)
            await reader.readline()  # OK MPD x.y.z
            writer.write(cmd + b"\n")
            await writer.drain()
            await reader.readline()  # OK
            writer.close()
            await writer.wait_closed()
        except Exception as e:
            logger.warning(f"MPD 命令 {cmd!r} 失败: {e}")

    async def _apply_default_volume(self, reason: str):
        volume = self._default_volume()
        await self._mpd_command(f"setvol {volume}".encode())
        logger.info("DLNA 默认音量已设置为 %s%% (%s)", volume, reason)

    async def _monitor_mpd_state(self):
        """通过 MPD idle 协议监控播放状态，变化时更新状态并联动优先级"""

        async def read_response(reader: asyncio.StreamReader) -> dict:
            result = {}
            while True:
                line = await reader.readline()
                if not line or line.startswith(b"OK") or line.startswith(b"ACK"):
                    break
                if b":" in line:
                    k, _, v = line.decode().partition(":")
                    result[k.strip()] = v.strip()
            return result

        while True:
            writer = None
            try:
                reader, writer = await asyncio.open_connection(MPD_HOST, MPD_PORT)
                await reader.readline()  # OK MPD x.y.z

                while True:
                    writer.write(b"idle player\n")
                    await writer.drain()
                    await read_response(reader)  # 等到 changed: player / OK

                    writer.write(b"status\n")
                    await writer.drain()
                    status = await read_response(reader)

                    mpd_state = status.get("state", "stop")
                    new_playing = mpd_state == "play"
                    new_streaming = mpd_state in ("play", "pause")

                    if new_streaming and not self._is_streaming:
                        await self._on_stream_start()
                    elif not new_streaming and self._is_streaming:
                        await self._on_stream_end()

                    self._is_playing = new_playing
                    self._is_streaming = new_streaming

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug(f"MPD 状态监控重连中: {e}")
                await asyncio.sleep(2)
            finally:
                if writer:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass

    async def _on_stream_start(self):
        logger.info("DLNA 开始串流")

        # 打断 AirPlay（平级优先级，后到打断先到）
        if self._airplay and self._airplay.is_playing:
            await self._airplay.pause()
            self._paused_airplay = True

        # 暂停背景音乐（若 AirPlay 已把它暂停，player.is_playing 为 False，幂等）
        from mediacenter.audio.manager import AudioPriority
        ch = self.manager.channels[AudioPriority.BACKGROUND]
        if ch.player.is_playing:
            ch.was_playing = True
            await ch.player.pause()

    async def _on_stream_end(self):
        logger.info("DLNA 停止串流")
        await self._apply_default_volume("stream ended")

        # 恢复被我们暂停的 AirPlay
        if self._paused_airplay and self._airplay:
            self._paused_airplay = False
            await self._airplay.resume()

        # 只在 AirPlay 也不在播放时才恢复背景音乐
        if not (self._airplay and self._airplay.is_playing):
            from mediacenter.audio.manager import AudioPriority
            ch = self.manager.channels[AudioPriority.BACKGROUND]
            if ch.was_playing:
                ch.was_playing = False
                await ch.player.resume()

    async def _wait_mpd_ready(self, timeout: float = 5.0):
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                _, writer = await asyncio.open_connection(MPD_HOST, MPD_PORT)
                writer.close()
                await writer.wait_closed()
                return
            except (ConnectionRefusedError, OSError):
                await asyncio.sleep(0.2)
        logger.warning("mpd 未在 %.1fs 内就绪，upmpdcli 可能连接失败", timeout)

    async def _monitor_output(self, proc, tag):
        async def _drain(stream, level):
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode(errors="ignore").strip()
                if text:
                    logger.log(level, f"DLNA[{tag}]: {text}")

        try:
            await asyncio.gather(
                _drain(proc.stdout, logging.DEBUG),
                _drain(proc.stderr, logging.WARNING),
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"DLNA[{tag}] 监控异常: {e}")

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    @property
    def is_streaming(self) -> bool:
        return self._is_streaming

    def get_status(self) -> dict:
        def alive(p):
            return p is not None and p.returncode is None

        return {
            "enabled": self.config.get("enabled", True),
            "running": alive(self._mpd_process) and alive(self._upmpdcli_process),
            "mpd_running": alive(self._mpd_process),
            "upmpdcli_running": alive(self._upmpdcli_process),
            "is_playing": self._is_playing,
            "name": self.config.get("name", "OpenWrt MediaCenter"),
        }

    async def stop(self):
        for task in self._monitor_tasks:
            task.cancel()
        for task in self._monitor_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._monitor_tasks.clear()

        for proc in (self._upmpdcli_process, self._mpd_process):
            if proc and proc.returncode is None:
                try:
                    proc.send_signal(signal.SIGTERM)
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (asyncio.TimeoutError, ProcessLookupError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass

        await self._proxy.stop()

        logger.info("DLNA 服务已停止")
