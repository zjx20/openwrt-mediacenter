"""AI 代理模块 - 支持工具调用控制媒体中心"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AIAgent:
    """AI 代理

    通过 OpenAI 兼容 API 提供智能对话能力，
    支持通过工具调用控制音乐播放、TTS 播报等功能。
    """

    def __init__(self, config: dict, media_center=None):
        self.config = config
        self.mc = media_center
        self.memory_file = Path(config.get("memory_file", "/tmp/ai_memory.json"))
        self._memories: list[dict] = []
        self._load_memories()

    def _load_memories(self):
        if self.memory_file.exists():
            try:
                self._memories = json.loads(self.memory_file.read_text())
            except (json.JSONDecodeError, OSError):
                self._memories = []

    def _save_memories(self):
        self.memory_file.parent.mkdir(parents=True, exist_ok=True)
        self.memory_file.write_text(json.dumps(self._memories, ensure_ascii=False, indent=2))

    def add_memory(self, content: str):
        """添加记忆"""
        self._memories.append({
            "time": datetime.now().isoformat(),
            "content": content,
        })
        # 保留最近 100 条
        self._memories = self._memories[-100:]
        self._save_memories()

    def _get_tools(self) -> list[dict]:
        """获取可用的工具定义"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "play_music",
                    "description": "播放音乐。支持 URL 或搜索关键词。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "音乐 URL 或搜索关键词",
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "control_playback",
                    "description": "控制音乐播放：pause/resume/stop/next/prev",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["pause", "resume", "stop", "next", "prev"],
                            },
                        },
                        "required": ["action"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "speak",
                    "description": "使用 TTS 朗读文本",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "要朗读的文本"},
                        },
                        "required": ["text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "set_volume",
                    "description": "设置音量 (0-100)",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "volume": {"type": "integer", "minimum": 0, "maximum": 100},
                        },
                        "required": ["volume"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_status",
                    "description": "获取媒体中心当前状态",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "remember",
                    "description": "记住一条信息以备后用",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "要记住的内容"},
                        },
                        "required": ["content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_script",
                    "description": "运行一段 Python 脚本来处理复杂任务",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string", "description": "Python 代码"},
                        },
                        "required": ["code"],
                    },
                },
            },
        ]

    async def _execute_tool(self, name: str, args: dict) -> str:
        """执行工具调用"""
        try:
            if name == "play_music":
                query = args["query"]
                if query.startswith(("http://", "https://")):
                    await self.mc.background_player.add_and_play(query)
                else:
                    from mediacenter.audio.sources import search_music
                    tracks = await search_music(query)
                    if tracks:
                        await self.mc.background_player.add_and_play(tracks[0].url)
                        return f"正在播放: {tracks[0].title}"
                    return "未找到匹配的音乐"
                return "已开始播放"

            elif name == "control_playback":
                action = args["action"]
                bp = self.mc.background_player
                actions = {
                    "pause": bp.pause,
                    "resume": bp.resume,
                    "stop": bp.stop,
                    "next": bp.next,
                    "prev": bp.prev,
                }
                if action in actions:
                    await actions[action]()
                    return f"已执行: {action}"
                return f"未知操作: {action}"

            elif name == "speak":
                await self.mc.tts.speak(args["text"])
                return "TTS 播报完成"

            elif name == "set_volume":
                from .audio.manager import AudioPriority
                await self.mc.audio_manager.set_volume(
                    AudioPriority.BACKGROUND, args["volume"]
                )
                return f"音量已设置为 {args['volume']}"

            elif name == "get_status":
                return json.dumps(self.mc.get_full_status(), ensure_ascii=False)

            elif name == "remember":
                self.add_memory(args["content"])
                return "已记住"

            elif name == "run_script":
                # 安全限制: 在受限环境中执行
                proc = await asyncio.create_subprocess_exec(
                    "python3", "-c", args["code"],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=30
                )
                output = stdout.decode() + stderr.decode()
                return output[:2000]

            return f"未知工具: {name}"
        except Exception as e:
            return f"执行出错: {e}"

    async def chat(self, user_message: str) -> str:
        """与 AI 对话"""
        import httpx

        api_key = self.config.get("api_key", "")
        base_url = self.config.get("base_url", "https://api.openai.com/v1")
        model = self.config.get("model", "gpt-4o")

        if not api_key:
            return "AI 功能未配置 API Key"

        # 构建上下文
        memory_context = ""
        if self._memories:
            recent = self._memories[-10:]
            memory_context = "\n你的记忆:\n" + "\n".join(
                f"- [{m['time']}] {m['content']}" for m in recent
            )

        system_prompt = self.config.get("system_prompt", "") + memory_context

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        async with httpx.AsyncClient(timeout=60) as client:
            # 初始请求
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": messages,
                    "tools": self._get_tools(),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]

            # 处理工具调用循环
            max_rounds = 5
            for _ in range(max_rounds):
                msg = choice["message"]
                if msg.get("tool_calls"):
                    messages.append(msg)
                    for tc in msg["tool_calls"]:
                        fn = tc["function"]
                        args = json.loads(fn["arguments"])
                        result = await self._execute_tool(fn["name"], args)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result,
                        })

                    resp = await client.post(
                        f"{base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={
                            "model": model,
                            "messages": messages,
                            "tools": self._get_tools(),
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    choice = data["choices"][0]
                else:
                    break

            return choice["message"].get("content", "")
