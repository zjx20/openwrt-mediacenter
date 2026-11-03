"""进程内的一次性定时停止任务。"""

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class StopTimer:
    def __init__(self, stop: Callable[[], Awaitable[None]]):
        self._stop = stop
        self._handle: asyncio.TimerHandle | None = None
        self._timestamp: float | None = None
        self._deadline: float | None = None
        self._tasks: set[asyncio.Task] = set()
        self._last_error: str | None = None

    def schedule(
        self, *, timestamp: float | None = None, duration: float | None = None
    ) -> dict:
        """设置或覆盖定时；先校验，避免错误请求取消已有任务。"""
        if (timestamp is None) == (duration is None):
            raise ValueError("timestamp 和 duration 必须且只能提供一个")
        value = timestamp if timestamp is not None else duration
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("时间必须是有限数字")
        if not math.isfinite(value):
            raise ValueError("时间必须是有限数字")

        now = time.time()
        delay = timestamp - now if timestamp is not None else duration
        stop_at = timestamp if timestamp is not None else now + duration
        if delay <= 0:
            raise ValueError("timestamp 必须在未来，duration 必须大于 0")
        if not math.isfinite(stop_at):
            raise ValueError("停止时间超出范围")

        loop = asyncio.get_running_loop()
        self.cancel()
        self._timestamp = stop_at
        self._deadline = loop.time() + delay
        self._last_error = None
        self._handle = loop.call_later(delay, self._fire)
        logger.info("已设置定时停止: timestamp=%s", stop_at)
        return self.get_status()

    def cancel(self) -> dict:
        """仅取消尚未触发的定时，不中断已经开始的停止操作。"""
        if self._handle is not None:
            self._handle.cancel()
        self._handle = None
        self._timestamp = None
        self._deadline = None
        return self.get_status()

    def get_status(self) -> dict:
        remaining = 0.0
        if self._deadline is not None:
            remaining = max(0.0, self._deadline - asyncio.get_running_loop().time())
        return {
            "active": self._handle is not None,
            "timestamp": self._timestamp,
            "remaining": remaining,
            "stopping": bool(self._tasks),
            "last_error": self._last_error,
        }

    def _fire(self):
        self._handle = None
        self._timestamp = None
        self._deadline = None
        task = asyncio.create_task(self._run(), name="stop-timer")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self):
        try:
            await self._stop()
            logger.info("定时停止已执行")
        except Exception as exc:
            self._last_error = str(exc)
            logger.exception("定时停止失败")

    async def shutdown(self):
        self.cancel()
        # 停止操作可能已经开始，等待收尾，避免只关闭部分播放器。
        if self._tasks:
            await asyncio.gather(*self._tasks)
