"""进程内后台任务追踪器。

统一收拢 `asyncio.create_task` 调用：
- 注册时立刻挂上 done callback，异常路径也能释放；
- 支持 shutdown 时 gather 等待所有 pending 任务结束，避免 lifespan finally 里
  `asyncio.aclose()` 撞上「生成器正在运行」。

用法：
    from core.tasktracker import tracker

    async def worker(): ...

    tracker.add(asyncio.create_task(worker(), name="worker"))

agent_bridge.shutdown() 会自动调 tracker.gather_all()。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("stocka.agent")


class TaskTracker:
    """后台任务单例：进程内唯一，多模块共用。"""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def add(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """注册一个任务，挂上自清理 callback。"""
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.warning(
                    "background task failed: %s",
                    task.get_name(),
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

    def pending_count(self) -> int:
        return len(self._tasks)

    def cancel_all(self) -> None:
        for t in list(self._tasks):
            t.cancel()

    async def gather_all(self) -> None:
        """等所有 pending 任务结束，超时或取消也不抛。"""
        if not self._tasks:
            return
        pending = list(self._tasks)
        try:
            async with asyncio.timeout(5.0):
                await asyncio.gather(*pending, return_exceptions=True)
        except TimeoutError:
            logger.warning(
                "TaskTracker gather timed out, %d tasks still pending",
                len(self._tasks),
            )
            self.cancel_all()
        self._tasks.clear()


# 进程级单例
_tracker = TaskTracker()


def tracker() -> TaskTracker:
    return _tracker
