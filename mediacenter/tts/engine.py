"""TTS 引擎 - 支持 edge-tts 和 OpenAI TTS"""

import asyncio
import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class TTSEngine:
    """文字转语音引擎

    支持多种后端:
    - edge-tts: 微软免费 TTS (推荐，无需 API Key)
    - openai: OpenAI TTS API
    - piper: 本地 Piper TTS (适合离线使用)
    """

    def __init__(self, audio_manager, config: dict):
        self.manager = audio_manager
        self.config = config
        self.cache_dir = Path(config.get("cache_dir", "/tmp/tts_cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._stream_pause_hook = None
        self._stream_resume_hook = None

    def set_stream_hooks(self, on_pause, on_resume):
        """注册 TTS 播放前后暂停/恢复 AirPlay+DLNA 的钩子（由 MediaCenter 注入）"""
        self._stream_pause_hook = on_pause
        self._stream_resume_hook = on_resume

    def _cache_path(self, text: str) -> Path:
        """生成缓存文件路径"""
        h = hashlib.md5(text.encode()).hexdigest()
        engine = self.config.get("engine", "edge-tts")
        voice = self.config.get("voice", "default")
        return self.cache_dir / f"{engine}_{voice}_{h}.mp3"

    async def synthesize(self, text: str) -> str:
        """合成语音，返回音频文件路径"""
        cache = self._cache_path(text)
        if cache.exists():
            logger.debug(f"TTS 缓存命中: {cache}")
            return str(cache)

        engine = self.config.get("engine", "edge-tts")

        if engine == "edge-tts":
            return await self._edge_tts(text, cache)
        elif engine == "openai":
            return await self._openai_tts(text, cache)
        elif engine == "piper":
            return await self._piper_tts(text, cache)
        else:
            raise ValueError(f"不支持的 TTS 引擎: {engine}")

    async def speak(self, text: str):
        """合成并播放 TTS (最高优先级)，播放前暂停 AirPlay/DLNA，结束后恢复"""
        from mediacenter.audio.manager import AudioPriority

        audio_path = await self.synthesize(text)

        if self._stream_pause_hook:
            await self._stream_pause_hook()

        await self.manager.play(AudioPriority.TTS, audio_path)
        player = self.manager.channels[AudioPriority.TTS].player
        await player.wait_for_end()
        await self.manager.stop(AudioPriority.TTS)

        if self._stream_resume_hook:
            await self._stream_resume_hook()

    async def _edge_tts(self, text: str, output: Path) -> str:
        """使用 edge-tts 合成"""
        voice = self.config.get("voice", "zh-CN-XiaoxiaoNeural")
        rate = self.config.get("rate", "+0%")

        try:
            import edge_tts

            communicate = edge_tts.Communicate(text, voice=voice, rate=rate)
            await communicate.save(str(output))
            logger.info(f"edge-tts 合成完成: {output}")
            return str(output)
        except ImportError:
            # fallback 到命令行
            cmd = [
                "edge-tts",
                "--voice", voice,
                "--rate", rate,
                "--text", text,
                "--write-media", str(output),
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"edge-tts 失败: {stderr.decode()}")
            return str(output)

    async def _openai_tts(self, text: str, output: Path) -> str:
        """使用 OpenAI TTS API"""
        import httpx

        openai_config = self.config.get("openai", {})
        api_key = openai_config.get("api_key", "")
        base_url = openai_config.get("base_url", "https://api.openai.com/v1")
        model = openai_config.get("model", "tts-1")
        voice = openai_config.get("voice", "alloy")

        if not api_key:
            raise ValueError("OpenAI TTS 需要配置 api_key")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/audio/speech",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "voice": voice, "input": text},
                timeout=30,
            )
            resp.raise_for_status()
            output.write_bytes(resp.content)

        logger.info(f"OpenAI TTS 合成完成: {output}")
        return str(output)

    async def _piper_tts(self, text: str, output: Path) -> str:
        """使用 Piper 本地 TTS"""
        proc = await asyncio.create_subprocess_exec(
            "piper",
            "--output_file", str(output),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate(input=text.encode())
        if proc.returncode != 0:
            raise RuntimeError(f"Piper TTS 失败: {stderr.decode()}")
        return str(output)

    def clear_cache(self):
        """清除 TTS 缓存"""
        count = 0
        for f in self.cache_dir.glob("*.mp3"):
            f.unlink()
            count += 1
        logger.info(f"已清除 {count} 个 TTS 缓存文件")
