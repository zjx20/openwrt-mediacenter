"""配置管理模块"""

import os
import yaml
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = {
    "server": {"host": "0.0.0.0", "port": 8080},
    "audio": {"backend": "pulse", "pulse_sink": "", "default_volume": 50},
    "background_music": {
        "auto_play": False,
        "playlist": [],
        "play_mode": "repeat_all",
    },
    "airplay": {
        "enabled": True,
        "name": "OpenWrt MediaCenter",
        "config_path": None,
        "default_volume": 80,
        "port": 5000,
    },
    "dlna": {
        "enabled": True,
        "name": "OpenWrt MediaCenter",
        "default_volume": 40,
        "port": 49152,
    },
    "tts": {
        "engine": "edge-tts",
        "voice": "zh-CN-XiaoxiaoNeural",
        "rate": "+0%",
        "pitch": "+0Hz",
        "cache_dir": "/tmp/tts_cache",
        "cache_max_mb": 50,
        "openai": {
            "api_key": "",
            "base_url": "https://api.openai.com/v1",
            "model": "tts-1",
            "voice": "alloy",
        },
    },
    "scheduler": {"jobs": []},
    "ai": {
        "enabled": False,
        "api_key": "",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "system_prompt": "你是一个智能家居助手，可以控制音乐播放、TTS 播报等。",
        "memory_file": "/etc/mediacenter/memory.json",
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典，override 覆盖 base"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    """全局配置单例"""

    _instance = None
    _data: dict = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def load(self, config_path: str | None = None):
        """加载配置文件"""
        self._data = DEFAULT_CONFIG.copy()

        if config_path is None:
            # 按优先级查找配置文件
            search_paths = [
                Path.cwd() / "config.yaml",
                Path.cwd() / "config.yml",
                Path("/etc/mediacenter/config.yaml"),
            ]
            for p in search_paths:
                if p.exists():
                    config_path = str(p)
                    break

        if config_path and os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                user_config = yaml.safe_load(f) or {}
            self._data = deep_merge(DEFAULT_CONFIG, user_config)

        # 确保 TTS 缓存目录存在
        os.makedirs(self._data["tts"]["cache_dir"], exist_ok=True)

    def get(self, *keys: str, default: Any = None) -> Any:
        """通过点分路径获取配置值，如 config.get('audio', 'device')"""
        value = self._data
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
                if value is None:
                    return default
            else:
                return default
        return value

    @property
    def data(self) -> dict:
        return self._data


config = Config()
