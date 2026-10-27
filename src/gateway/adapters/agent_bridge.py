"""与 agent（LangGraph + RAG）在同一进程内合流的桥接层。

合并前网关是通过 HTTP 访问 agent 的 /run/stream（ndjson）与 /sessions；
合并后同进程，直接把事件生成器接进 SSE 路由、直接调用会话函数，
省掉一次 HTTP 往返、一次 ndjson 序列化/反序列化和一套 2048/1MB 限长逻辑。

导入约束：两侧都在 src/ 下，agent 一律用 `agent.*` 绝对导入、
网关一律用 `gateway.*`，不再靠运行期改动 sys.path 来共享包根。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncGenerator
from typing import Any

from gateway.infra.deps import Runtime

logger = logging.getLogger("stocka.gateway.agent_bridge")

_ready = False  # 预热完成（图/工具可用）
_started = False  # startup() 已被调用过（决定收尾时要不要关池）
_warm_task: asyncio.Task | None = None
_cleanup_task: asyncio.Task | None = None
# 主图建好即可服务（取数工具热身只影响首轮延迟），单独用事件标记，
# 让重启后的首批提问能先等它，而不是被 run_timeout 直接判超时
_graph_ready = asyncio.Event()


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
    from agent.config import CHECKPOINT_CLEANUP_INTERVAL_HOURS
    from agent.core.sessions import prune_orphan_threads

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
    """只做必须同步完成的最小工作，其余预热丢到后台。

    单进程后 uvicorn 要等 lifespan 返回才开始 accept 连接。原先在这里同步跑
    图 / checkpointer / tokenizer / 取数工具热身——生产实测从建表到就绪 128s，
    等于每次重启都有两分钟 502。

    现在只同步做「配置映射」这一步（agent/config.py 在导入期就读这些环境变量，
    必须早于任何 agent 模块的 import），其余交给 `_warmup` 后台跑：
    站点立刻可服务，首个提问会自动等图建好（get_graph / get_checkpointer 都带锁，
    不会重复初始化）。
    """
    global _started, _warm_task

    if _started:
        return
    _started = True
    _sync_env(rt)
    _warm_task = asyncio.create_task(_warmup(), name="agent-warmup")
    logger.info("agent warmup scheduled in background")


async def _warmup() -> None:
    """后台预热：图（含 checkpointer 建表）、tokenizer、会话表、取数工具。"""
    global _ready, _cleanup_task

    t0 = time.monotonic()
    from agent.core.graph import get_graph  # 预热图（内部会建 checkpointer 连接池）
    from agent.core.sessions import ensure_sessions_table

    try:
        await get_graph()
        _graph_ready.set()
    except Exception as exc:  # noqa: BLE001 - 预热失败不阻断启动，首个请求会再试
        logger.warning("graph warmup failed: %s", exc)

    # 首次 encode 要加载词表（实测约 3.9s），不预热会把它算到首个请求的 guardrail 上
    try:
        from agent.core.llm import estimate_tokens

        estimate_tokens("预热")
    except Exception as exc:  # noqa: BLE001
        logger.warning("tokenizer warmup failed: %s", exc)

    try:
        await ensure_sessions_table()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure chat_sessions failed: %s", exc)

    # 取数工具热身要走 akshare/baostock，生产上这里是耗时大头（数十秒）
    try:
        from agent.subgraphs.stock import warm_tools

        logger.info("stock tools warmed: %d", await warm_tools())
    except Exception as exc:  # noqa: BLE001
        logger.warning("tool warmup failed: %s", exc)

    _cleanup_task = asyncio.create_task(_cleanup_loop(), name="checkpoint-cleanup")
    _ready = True
    logger.info("agent bridge ready (%.1fs)", time.monotonic() - t0)


async def shutdown() -> None:
    global _ready, _started, _warm_task, _cleanup_task

    # 预热可能还在跑（刚重启就被关停）：先取消，别让它在池子关掉之后再去建池/打网络
    if _warm_task is not None:
        _warm_task.cancel()
        try:
            await _warm_task
        except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001
            logger.info("agent warmup cancelled: %s", exc)
        _warm_task = None

    # 等所有后台任务先收口，再关连接池，防止还有任务在写库/写 Redis
    try:
        from agent.core.tasktracker import tracker
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

    if not _started:
        return

    # 模块名必须写全（agent.*）：这里是 __import__ 的字符串，包根改名时静态检查发现不了
    #（src 布局改造后这里曾失效一整轮，表现为「关停时池子根本没关」）
    closer = (
        ("agent.core.checkpointer", "close_checkpointer"),
        ("agent.storage.read", "close_read_pool"),
        ("agent.storage.app", "close_app_pool"),
        ("agent.core.redis", "close_redis"),
        ("agent.tools", "close_http_client"),
        ("agent.rag.reranker", "close_rerank_client"),
    )
    for module_name, attr in closer:
        try:
            module = __import__(module_name, fromlist=[attr])
            await getattr(module, attr)()
        except Exception as exc:  # noqa: BLE001 - 收尾失败不影响进程退出
            logger.warning("close %s.%s failed: %s", module_name, attr, exc)
    _ready = False
    _started = False


# ---------------------------------------------------------------- 运行与事件流


async def stream_events(
    thread_id: str, query: str, user_id: str
) -> AsyncGenerator[dict[str, Any], None]:
    """驱动一次问答，产出 {"event":..., "data":...}。

    与旧的 ndjson 通道相比少了一层序列化，但事件名与 data 结构一字不改，
    前端的 SSE 解析逻辑无需变动。
    """
    from agent.core.events import stream_events as _stream

    async for event in _stream(thread_id, query, user_id):
        yield event


def is_ready() -> bool:
    """主图与取数工具是否都已预热（仅供观测/测试使用）。"""
    return _ready


async def wait_graph_ready(max_wait: float) -> None:
    """等主图建好，最长 max_wait 秒；超时或未启动预热时直接返回。

    重启后有两分钟冷启动期：这段时间进来的提问如果照常开始计 run_timeout，
    会被自己的超时判死。这里先把「建图」等掉，再开始计预算。
    预热没被调度（如契约冒烟只跑网关）时立刻返回，不能把请求挂住。
    """
    if not _started or _graph_ready.is_set():
        return
    try:
        await asyncio.wait_for(_graph_ready.wait(), max_wait)
    except (TimeoutError, asyncio.CancelledError):
        # 超时/取消都放行：请求自己会再触发一次 get_graph（带锁，不会重复建）
        pass


def track_task(coro: Any, *, name: str) -> asyncio.Task:
    """把旁路任务登记到 agent 侧 tracker（关停时会等它收口）。

    agent 未能加载时（如契约冒烟只跑网关）退化为裸 create_task，
    不因为一个旁路任务让主链路失败。
    """
    try:
        from agent.core.tasktracker import tracker

        return tracker().add(asyncio.create_task(coro, name=name))
    except Exception as exc:  # noqa: BLE001
        logger.info("track_task fallback (%s): %s", name, exc)
        return asyncio.create_task(coro, name=name)


# ---------------------------------------------------------------- 会话历史


async def list_sessions(user_id: str) -> list[dict[str, Any]]:
    from agent.core.sessions import list_sessions as _list

    return await _list(user_id)


async def upsert_session(user_id: str, session_id: str, title: str) -> None:
    from agent.core.sessions import upsert_session as _upsert

    await _upsert(user_id, session_id, title, auto_title=not title.strip())


async def session_messages(user_id: str, session_id: str) -> list[dict[str, Any]]:
    from agent.core.sessions import session_messages as _messages

    return await _messages(user_id, session_id)


async def delete_session(user_id: str, session_id: str) -> bool:
    from agent.core.sessions import delete_session as _delete

    return await _delete(user_id, session_id)


def valid_session_id(session_id: str) -> bool:
    from agent.core.sessions import valid_session_id as _valid

    return _valid(session_id)


def thread_id_of(user_id: str, session_id: str) -> str:
    """thread_id 口径与 agent 侧一致：default:{user_id}:{session_id}。"""
    from agent.core.sessions import thread_id_of as _tid

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
    from gateway.services import stock as stock_service

    try:
        if path == "/api/v1/stocks/search":
            data = await stock_service.service_search(
                keyword=str((params or {}).get("q") or ""),
                limit=int((params or {}).get("limit") or 10),
            )
        elif path == "/api/v1/stock/quote":
            data = await stock_service.service_quote(
                code=str((params or {}).get("code") or "")
            )
        else:
            return {"ok": False, "error": f"unsupported internal path: {path}"}
    except Exception as exc:  # noqa: BLE001 - 与 HTTP 版语义一致：失败返回错误结构
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "data": data}
