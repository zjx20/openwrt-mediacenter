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

    支持的中文音色（engine=edge-tts）：
      zh-HK-HiuMaanNeural           曉曼 粤语 女（默认）
      zh-HK-HiuGaaiNeural           曉佳 粤语 女
      zh-HK-WanLungNeural           雲龍 粤语 男
      zh-CN-XiaoxiaoNeural          晓晓 中文 女
      zh-CN-XiaoyiNeural            晓依 中文 女
      zh-CN-YunjianNeural           云健 中文 男
      zh-CN-YunxiNeural             云希 中文 男
      zh-CN-YunxiaNeural            云夏 中文 男
      zh-CN-YunyangNeural           云扬 中文 男
      zh-CN-liaoning-XiaobeiNeural  晓北 辽宁 女
      zh-CN-shaanxi-XiaoniNeural    晓妮 陕西 女
    """

    def __init__(self, audio_manager, config: dict):
        self.manager = audio_manager
        self.config = config
        self.cache_dir = Path(config.get("cache_dir", "/tmp/tts_cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.api_base: str = ""

    def _cache_path(self, text: str) -> Path:
        """生成缓存文件路径"""
        h = hashlib.md5(text.encode()).hexdigest()
        engine = self.config.get("engine", "edge-tts")
        voice = self.config.get("voice", "default")
        return self.cache_dir / f"{engine}_{voice}_{h}.mp3"

    def _evict_cache(self):
        """LRU 淘汰：按 atime 升序删除最久未访问文件，直到总大小低于 cache_max_mb"""
        max_mb = self.config.get("cache_max_mb", 50)
        if not max_mb:
            return
        max_bytes = max_mb * 1024 * 1024
        files = sorted(self.cache_dir.glob("*.mp3"), key=lambda f: f.stat().st_atime)
        total = sum(f.stat().st_size for f in files)
        while total > max_bytes and files:
            oldest = files.pop(0)
            total -= oldest.stat().st_size
            oldest.unlink()
            logger.debug(f"LRU 淘汰缓存: {oldest.name}")

    async def synthesize(self, text: str) -> str:
        """合成语音，返回音频文件路径"""
        cache = self._cache_path(text)
        if cache.exists():
            logger.debug(f"TTS 缓存命中: {cache}")
            return str(cache)

        engine = self.config.get("engine", "edge-tts")

        if engine == "edge-tts":
            path = await self._edge_tts(text, cache)
        elif engine == "openai":
            path = await self._openai_tts(text, cache)
        elif engine == "piper":
            path = await self._piper_tts(text, cache)
        else:
            raise ValueError(f"不支持的 TTS 引擎: {engine}")

        self._evict_cache()
        return path

    async def stream(self, text: str):
        """流式合成：命中缓存从文件读，否则 edge_tts 边 yield 边写缓存，完成后 LRU 淘汰"""
        cache = self._cache_path(text)
        if cache.exists():
            cache.touch()  # 更新 atime 供 LRU 使用
            with open(cache, "rb") as f:
                while data := f.read(8192):
                    yield data
            return

        engine = self.config.get("engine", "edge-tts")
        if engine != "edge-tts":
            # 非 edge-tts 不支持流式，降级到 synthesize
            path = await self.synthesize(text)
            with open(path, "rb") as f:
                while data := f.read(8192):
                    yield data
            return

        import edge_tts

        voice = self.config.get("voice", "zh-HK-HiuMaanNeural")
        rate  = self.config.get("rate",  "+0%")
        pitch = self.config.get("pitch", "+0Hz")
        tmp   = cache.with_suffix(".tmp")

        communicate = edge_tts.Communicate(text, voice=voice, rate=rate, pitch=pitch)
        try:
            with open(tmp, "wb") as f:
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
                        yield chunk["data"]
            tmp.rename(cache)
            self._evict_cache()
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    async def speak(self, text: str):
        """合成并播放 TTS。

        TTS 不再显式打断其他音频流：mpv TTS 通道发出的流带有 PulseAudio
        media.role=tts，由系统层 module-role-ducking 自动把 background /
        airplay / dlna 等角色的音量压低，播报完成后自动恢复。
        """
        import urllib.parse
        from mediacenter.audio.manager import AudioPriority

        if self.api_base:
            params = urllib.parse.urlencode({"text": text})
            audio_url = f"{self.api_base}/api/tts/stream?{params}"
        else:
            # fallback：api_base 未注入时（测试场景）先完整合成再播
            audio_url = await self.synthesize(text)

        await self.manager.play(AudioPriority.TTS, audio_url)
        player = self.manager.channels[AudioPriority.TTS].player
        await player.wait_for_end()
        await self.manager.stop(AudioPriority.TTS)

    async def _edge_tts(self, text: str, output: Path) -> str:
        """使用 edge-tts 合成"""
        voice = self.config.get("voice", "zh-HK-HiuMaanNeural")
        rate  = self.config.get("rate",  "+0%")
        pitch = self.config.get("pitch", "+0Hz")

        try:
            import edge_tts

            communicate = edge_tts.Communicate(text, voice=voice, rate=rate, pitch=pitch)
            await communicate.save(str(output))
            logger.info(f"edge-tts 合成完成: {output}")
            return str(output)
        except ImportError:
            # fallback 到命令行
            cmd = [
                "edge-tts",
                "--voice", voice,
                "--rate", rate,
                "--pitch", pitch,
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
