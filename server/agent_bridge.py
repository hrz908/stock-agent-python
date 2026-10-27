"""与 agent（LangGraph + RAG）在同一进程内合流的桥接层。

合并前网关是通过 HTTP 访问 agent 的 /run/stream（ndjson）与 /sessions；
合并后同进程，直接把事件生成器接进 SSE 路由、直接调用会话函数，
省掉一次 HTTP 往返、一次 ndjson 序列化/反序列化和一套 2048/1MB 限长逻辑。

导入约束：agent 侧的顶层模块名是 core / tools / subgraphs / config / db …
网关自己的模块一律走 server.* 绝对导入，避免两边同名模块互相劫持。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from server.deps import Runtime

logger = logging.getLogger("stocka.server.agent_bridge")

AGENT_ROOT = Path(__file__).resolve().parent.parent / "agent"

_ready = False
_cleanup_task: asyncio.Task | None = None


def _ensure_agent_path() -> None:
    root = str(AGENT_ROOT)
    if root not in sys.path:
        # 插到最前：agent 侧模块按「项目根 = agent/」的方式互引
        sys.path.insert(0, root)


def _redis_url(cfg: Any) -> str:
    addr = (cfg.redis_addr or "127.0.0.1:6379").strip()
    if addr.startswith(("redis://", "rediss://")):
        return addr
    auth = f":{cfg.redis_password}@" if cfg.redis_password else ""
    return f"redis://{auth}{addr}/{cfg.redis_db}"


def _sync_env(rt: Runtime) -> None:
    """把网关配置映射到 agent 侧读取的环境变量。

    用 setdefault：真实环境变量优先，不覆盖运维显式设置的值。
    """
    os.environ.setdefault("DATABASE_URL", rt.cfg.dsn)
    os.environ.setdefault("REDIS_URL", _redis_url(rt.cfg))


async def _cleanup_loop() -> None:
    """后台回收孤儿 checkpoint（benchmark / 调试线程）。

    启动 60s 后跑一次，之后按配置间隔循环；失败只记日志，不影响服务。
    """
    from config import CHECKPOINT_CLEANUP_INTERVAL_HOURS
    from core.sessions import prune_orphan_threads

    await asyncio.sleep(60)
    while True:
        try:
            removed = await prune_orphan_threads()
            logger.info("checkpoint cleanup: removed %d orphan threads", removed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkpoint cleanup failed: %s", exc)
        if CHECKPOINT_CLEANUP_INTERVAL_HOURS <= 0:
            return
        await asyncio.sleep(CHECKPOINT_CLEANUP_INTERVAL_HOURS * 3600)


async def startup(rt: Runtime) -> None:
    """预热 agent 侧资源（图、checkpointer、tiktoken、工具集、会话表）。"""
    global _ready, _cleanup_task

    if _ready:
        return
    _ensure_agent_path()
    _sync_env(rt)

    from core.graph import get_graph  # 预热图（内部会建 checkpointer 连接池）
    from core.sessions import ensure_sessions_table

    try:
        await get_graph()
    except Exception as exc:  # noqa: BLE001 - 预热失败不阻断启动，首个请求会再试
        logger.warning("graph warmup failed: %s", exc)

    # 首次 encode 要加载词表（实测约 3.9s），不预热会把它算到首个请求的 guardrail 上
    try:
        from core.llm import estimate_tokens

        estimate_tokens("预热")
    except Exception as exc:  # noqa: BLE001
        logger.warning("tokenizer warmup failed: %s", exc)

    try:
        await ensure_sessions_table()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure chat_sessions failed: %s", exc)

    try:
        from subgraphs.stock import warm_tools

        logger.info("stock tools warmed: %d", await warm_tools())
    except Exception as exc:  # noqa: BLE001
        logger.warning("tool warmup failed: %s", exc)

    _cleanup_task = asyncio.create_task(_cleanup_loop())
    _ready = True
    logger.info("agent bridge ready")


async def shutdown() -> None:
    global _ready, _cleanup_task

    # 等所有后台任务先收口，再关连接池，防止还有任务在写库/写 Redis
    try:
        from core.tasktracker import tracker
        await tracker().gather_all()
    except Exception as exc:  # noqa: BLE001
        logger.info("tasktracker gather failed: %s", exc)

    if _cleanup_task is not None:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001
            logger.info("checkpoint cleanup task stopped: %s", exc)
        _cleanup_task = None

    if not _ready:
        return

    closer = (
        ("core.checkpointer", "close_checkpointer"),
        ("db", "close_pool"),
        ("core.pg", "close_pool"),
        ("core.redis", "close_redis"),
        ("tools", "close_http_client"),
        ("rag.reranker", "close_rerank_client"),
    )
    for module_name, attr in closer:
        try:
            module = __import__(module_name, fromlist=[attr])
            await getattr(module, attr)()
        except Exception as exc:  # noqa: BLE001 - 收尾失败不影响进程退出
            logger.warning("close %s.%s failed: %s", module_name, attr, exc)
    _ready = False


# ---------------------------------------------------------------- 运行与事件流


async def stream_events(
    thread_id: str, query: str, user_id: str
) -> AsyncGenerator[dict[str, Any], None]:
    """驱动一次问答，产出 {"event":..., "data":...}。

    与旧的 ndjson 通道相比少了一层序列化，但事件名与 data 结构一字不改，
    前端的 SSE 解析逻辑无需变动。
    """
    from app.main import stream_events as _stream

    async for event in _stream(thread_id, query, user_id):
        yield event


# ---------------------------------------------------------------- 会话历史


async def list_sessions(user_id: str) -> list[dict[str, Any]]:
    from core.sessions import list_sessions as _list

    return await _list(user_id)


async def upsert_session(user_id: str, session_id: str, title: str) -> None:
    from core.sessions import upsert_session as _upsert

    await _upsert(user_id, session_id, title, auto_title=not title.strip())


async def session_messages(user_id: str, session_id: str) -> list[dict[str, Any]]:
    from core.sessions import session_messages as _messages

    return await _messages(user_id, session_id)


async def delete_session(user_id: str, session_id: str) -> bool:
    from core.sessions import delete_session as _delete

    return await _delete(user_id, session_id)


def valid_session_id(session_id: str) -> bool:
    from core.sessions import valid_session_id as _valid

    return _valid(session_id)


def thread_id_of(user_id: str, session_id: str) -> str:
    """thread_id 口径与 agent 侧一致：default:{user_id}:{session_id}。"""
    from core.sessions import thread_id_of as _tid

    return _tid(user_id, session_id)


def truncate_runes(text: str, limit: int) -> str:
    """按字符（而非字节）截断，避免中文标题被拦腰切成乱码。"""
    stripped = (text or "").strip()
    return stripped if len(stripped) <= limit else stripped[:limit]


# -------------------------------------------------- 供 agent 工具层直调的业务能力


async def go_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """替代 agent 侧原本对网关的 HTTP 调用（GO_SERVER_URL / GO_SERVER_TOKEN）。

    直接调用本进程的业务函数，返回统一的 {ok, data} / {ok:false, error} 结构，
    与 agent/tools/__init__.py 里 go_get 的既有契约一致。
    """
    from server.modules import stock as stock_module

    try:
        if path == "/api/v1/stocks/search":
            data = await stock_module.service_search(
                keyword=str((params or {}).get("q") or ""),
                limit=int((params or {}).get("limit") or 10),
            )
        elif path == "/api/v1/stock/quote":
            data = await stock_module.service_quote(
                code=str((params or {}).get("code") or "")
            )
        else:
            return {"ok": False, "error": f"unsupported internal path: {path}"}
    except Exception as exc:  # noqa: BLE001 - 与 HTTP 版语义一致：失败返回错误结构
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "data": data}
