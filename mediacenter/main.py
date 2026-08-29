"""OpenWrt 媒体中心 - 主入口"""

import argparse
import asyncio
import logging
import signal
import sys

import uvicorn

from .config import config
from .audio.manager import AudioManager, AudioPriority
from .audio.background import BackgroundPlayer
from .airplay.receiver import AirPlayReceiver
from .dlna.renderer import DLNARenderer
from .tts.engine import TTSEngine
from .api.routes import app, init_api

logger = logging.getLogger(__name__)


class MediaCenter:
    """媒体中心主服务"""

    def __init__(self):
        self.audio_manager: AudioManager | None = None
        self.background_player: BackgroundPlayer | None = None
        self.airplay: AirPlayReceiver | None = None
        self.dlna: DLNARenderer | None = None
        self.tts: TTSEngine | None = None

    async def start(self):
        """启动所有服务"""
        backend = config.get("audio", "backend", default="pulse")
        pulse_sink = config.get("audio", "pulse_sink", default="")

        # 1. 音频管理器
        self.audio_manager = AudioManager(backend=backend, pulse_sink=pulse_sink)
        default_vol = config.get("audio", "default_volume", default=50)
        await self.audio_manager.set_volume(AudioPriority.BACKGROUND, default_vol)
        tts_vol = config.get("tts", "volume", default=70)
        await self.audio_manager.set_volume(AudioPriority.TTS, tts_vol)
        logger.info(f"音频管理器已启动 (backend={backend})")

        # 2. 背景音乐播放器
        self.background_player = BackgroundPlayer(self.audio_manager)
        logger.info("背景音乐播放器已启动")

        # 3. TTS 引擎
        self.tts = TTSEngine(self.audio_manager, config.get("tts", default={}))
        port = config.get("server", "port", default=8080)
        self.tts.api_base = f"http://127.0.0.1:{port}"
        logger.info("TTS 引擎已启动")

        # 4. AirPlay
        self.airplay = AirPlayReceiver(
            self.audio_manager, config.get("airplay", default={})
        )
        await self.airplay.start()

        # 5. DLNA
        self.dlna = DLNARenderer(
            self.audio_manager, config.get("dlna", default={})
        )
        await self.dlna.start()

        # 5b. 互相注入 peer 引用，启用 AirPlay ↔ DLNA 互斥硬重启
        self.airplay._dlna = self.dlna
        self.dlna._airplay = self.airplay

        # 注：TTS 不再显式暂停 AirPlay / DLNA / 背景音乐，
        # 改由 PulseAudio module-role-ducking 在系统层做音量自动压制。

        # 6. 自动播放背景音乐
        bg_config = config.get("background_music", default={})
        if bg_config.get("auto_play") and bg_config.get("playlist"):
            play_mode = bg_config.get("play_mode", "repeat_all")
            await self.background_player.load_playlist(
                bg_config["playlist"], play_mode
            )
            await self.background_player.start()
            logger.info("自动播放背景音乐")

        # 注册 API
        init_api(self)
        logger.info("=== OpenWrt 媒体中心已启动 ===")

    def get_full_status(self) -> dict:
        """获取完整系统状态"""
        return {
            "audio_channels": self.audio_manager.get_status()
            if self.audio_manager
            else {},
            "background_music": self.background_player.get_status()
            if self.background_player
            else {},
            "airplay": self.airplay.get_status() if self.airplay else {},
            "dlna": self.dlna.get_status() if self.dlna else {},
        }

    async def shutdown(self):
        """关闭所有服务"""
        logger.info("正在关闭媒体中心...")
        if self.airplay:
            await self.airplay.stop()
        if self.dlna:
            await self.dlna.stop()
        if self.audio_manager:
            await self.audio_manager.shutdown()
        logger.info("媒体中心已关闭")


_mc = MediaCenter()


@app.on_event("startup")
async def on_startup():
    await _mc.start()


@app.on_event("shutdown")
async def on_shutdown():
    await _mc.shutdown()


def main():
    parser = argparse.ArgumentParser(description="OpenWrt 媒体中心")
    parser.add_argument(
        "-c", "--config", help="配置文件路径", default=None
    )
    parser.add_argument(
        "-H", "--host", help="监听地址", default=None
    )
    parser.add_argument(
        "-p", "--port", help="监听端口", type=int, default=None
    )
    parser.add_argument(
        "-v", "--verbose", help="详细日志", action="store_true"
    )
    args = parser.parse_args()

    # 配置日志
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 只打印 4xx/5xx access log，过滤掉 2xx/3xx 的噪音
    class _ErrorOnlyAccessFilter(logging.Filter):
        def filter(self, record):
            return bool(record.args) and record.args[-1] >= 400

    logging.getLogger("uvicorn.access").addFilter(_ErrorOnlyAccessFilter())

    # 加载配置
    config.load(args.config)

    host = args.host or config.get("server", "host", default="0.0.0.0")
    port = args.port or config.get("server", "port", default=8080)
    # 将命令行参数解析出的最终值写回 config，供 start() 中各组件读取
    config._data["server"]["host"] = host
    config._data["server"]["port"] = port

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info" if not args.verbose else "debug",
    )


if __name__ == "__main__":
    main()
