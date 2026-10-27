"""进程内事件钩子：模块之间不互相 import，靠这里广播。

为什么必须用「脱离请求上下文」的独立任务：行情刷新请求返回后请求 ctx 就被取消，
提醒检查跑到一半会被一起掐死，用户就收不到推送。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger("stocka.server.hook")

QuoteUpdatedHandler = Callable[[int], Awaitable[None]]

_handlers: list[QuoteUpdatedHandler] = []
_registered: set[str] = set()
_background: set[asyncio.Task] = set()


def register(name: str, handler: QuoteUpdatedHandler) -> None:
    """按名字幂等注册：同一个检查器只会订阅一次，否则一条提醒会被检查两遍。"""
    if name in _registered:
        return
    _registered.add(name)
    _handlers.append(handler)


async def _safe_run(handler: QuoteUpdatedHandler, user_id: int) -> None:
    try:
        await handler(user_id)
    except Exception:  # noqa: BLE001 - 单个钩子崩溃不能影响其它订阅者
        logger.exception("hook handler failed: user_id=%s", user_id)


def emit_quote_updated_async(user_id: int) -> None:
    """异步广播「行情已更新」，不阻塞当前请求。"""
    for handler in list(_handlers):
        task = asyncio.create_task(_safe_run(handler, user_id))
        _background.add(task)
        task.add_done_callback(_background.discard)


def clear() -> None:
    _handlers.clear()
    _registered.clear()
