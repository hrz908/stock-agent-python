"""对话与历史会话接口。

合并后这里不再代理 HTTP，而是直接把 agent 的事件生成器接到 SSE 上：
一次问答少一跳 HTTP、少一次 ndjson 序列化/解析，但对外契约一字不改。

对外契约（破了前端就白屏）：
- Content-Type 必须是 `text/event-stream`（小程序靠它判断走没走 SSE）；
- 每帧 `event: <type>\\ndata: <单行 JSON>\\n\\n` 并立即 flush；
- `X-Accel-Buffering: no`，否则 nginx 会把事件攒批下发、失去实时性；
- 限流拒绝发生在写流头之前，对外是标准 JSON 429。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from server import agent_bridge
from server.agent_limiter import (
    ERR_QUOTA_EXCEEDED,
    ERR_TOO_MANY_CONCURRENT,
    AgentLimiter,
)
from server.context import current_request_id
from server.deps import runtime
from server.resp import (
    CODE_BAD_GATEWAY,
    CODE_BAD_REQUEST,
    CODE_TOO_MANY_REQUESTS,
    ApiError,
    ok_response,
)
from server.security.auth import CurrentUser
from server.web import read_json

logger = logging.getLogger("stocka.server.chat")

router = APIRouter(prefix="/agent", tags=["agent"])

# FastAPI Annotated 依赖别名：agent_limiter 通过 runtime 服务容器注入
AgentLimiterDep = Annotated[
    AgentLimiter, Depends(lambda: runtime().service("agent_limiter"))
]

STREAM_BODY_LIMIT = 64 << 10
MAX_QUERY_RUNES = 2000
MAX_SESSION_ID_LEN = 64
SESSION_TITLE_RUNES = 40


def _run_semaphore() -> asyncio.Semaphore:
    return runtime().service("run_semaphore")


def _valid_session_id(session_id: str) -> bool:
    """session_id 会拼进 thread_id 与 Redis key，只放行安全字符。

    与 agent 侧 valid_session_id 同口径，避免两边判定不一致导致 400/500 摇摆。
    """
    text = session_id or ""
    if not text or len(text) > MAX_SESSION_ID_LEN:
        return False
    return all(
        ch.isascii() and (ch.isalnum() or ch in ("_", "-")) for ch in text
    )


def _trim(text: Any) -> str:
    return text.strip() if isinstance(text, str) else ""


def _frame(event: str, data: Any) -> bytes:
    try:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        # 序列化失败降级为 null，不中断整条流
        payload = "null"
    return f"event: {event}\ndata: {payload}\n\n".encode()


def _reject_message(code: str | None) -> str:
    if code == ERR_TOO_MANY_CONCURRENT:
        return "当前同时进行的对话过多，请稍后再试"
    if code == ERR_QUOTA_EXCEEDED:
        return "今日提问次数已用完，明天再来吧"
    return "请求过于频繁，请稍后再试"


async def _register_session(user_id: int, session_id: str, query: str) -> None:
    """把本轮会话 upsert 进注册表，标题取首个问题。

    旁路逻辑：失败只记日志，不阻断对话流。
    """
    if not session_id:
        return
    try:
        await asyncio.wait_for(
            agent_bridge.upsert_session(
                str(user_id), session_id, agent_bridge.truncate_runes(query, SESSION_TITLE_RUNES)
            ),
            timeout=2.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("register session %s failed: %s", session_id, exc)


async def _sse_stream(
    user_id: int, session_id: str, query: str, limiter: AgentLimiter
) -> AsyncGenerator[bytes, None]:
    cfg = runtime().cfg
    run_timeout = cfg.agent.run_timeout
    ttft_timeout = cfg.agent.ttft_timeout
    thread_id = f"default:{user_id}:{session_id}"

    loop = asyncio.get_running_loop()
    deadline = loop.time() + run_timeout
    semaphore = _run_semaphore()

    try:
        # 上游并发闸门：容量 proxy_concurrency
        async with semaphore:
            agen = agent_bridge.stream_events(thread_id, query, str(user_id))
            iterator = agen.__aiter__()
            first = True
            finished = False
            next_task: asyncio.Task | None = None

            try:
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise TimeoutError("agent run timeout")

                    # 首字节等待单独设上限：超时先推一帧安抚前端，不取消上游任务。
                    # 必须复用同一个 next_task：重复 create_task(iterator.__anext__())
                    # 会抛 RuntimeError: anext(): asynchronous generator is already running
                    # （评测实测：首个事件超过 ttft_timeout=8s 时整轮失败）。
                    timeout = min(ttft_timeout, remaining) if first else remaining
                    if next_task is None:
                        next_task = asyncio.create_task(iterator.__anext__())
                    done, _ = await asyncio.wait({next_task}, timeout=timeout)

                    if not done:
                        # 定时器只触发一次：收到首个事件后 first 变 False
                        if first:
                            yield _frame(
                                "status", {"stage": "thinking", "label": "正在思考…"}
                            )
                            first = False
                        continue

                    task, next_task = next_task, None
                    try:
                        event = task.result()
                    except StopAsyncIteration:
                        finished = True
                        break

                    first = False
                    if not isinstance(event, dict):
                        continue
                    name = str(event.get("event") or "")
                    if not name:
                        continue
                    yield _frame(name, event.get("data"))

            except TimeoutError:
                logger.info("agent run timeout: thread=%s", thread_id)
                yield _frame(
                    "error", {"code": "agent_error", "msg": "对话超时，请稍后重试"}
                )
            except asyncio.CancelledError:
                # 客户端断开：向上抛出，让上层取消上游生成器
                raise
            except Exception as exc:
                logger.exception("agent stream failed: thread=%s", thread_id)
                yield _frame(
                    "error", {"code": "agent_error", "msg": f"{type(exc).__name__}: {exc}"}
                )
            finally:
                # 先收掉可能还挂着的 __anext__（否则 aclose 会撞上「生成器正在运行」）
                if next_task is not None and not next_task.done():
                    next_task.cancel()
                    try:
                        await next_task
                    except (asyncio.CancelledError, StopAsyncIteration):
                        pass
                # 客户端中途断开时，显式关闭上游生成器，释放图与连接资源
                if not finished:
                    try:
                        await iterator.aclose()
                    except Exception as exc:  # noqa: BLE001 - 关闭上游失败不影响已发出的帧
                        logger.info("close agent stream failed: thread=%s err=%s", thread_id, exc)
    finally:
        await limiter.release_concurrent(user_id)


@router.post("/stream")
async def agent_stream(
    request: Request,
    user_id: CurrentUser,
    limiter: AgentLimiterDep,
):
    payload = await read_json(request, STREAM_BODY_LIMIT, "请求体格式错误")
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ApiError(CODE_BAD_REQUEST, "query 不能为空")
    if len(query.strip()) > MAX_QUERY_RUNES:
        raise ApiError(
            CODE_BAD_REQUEST, f"提问过长，最多 {MAX_QUERY_RUNES} 字"
        )

    session_id = _trim(payload.get("session_id"))
    if session_id and not _valid_session_id(session_id):
        raise ApiError(CODE_BAD_REQUEST, "session_id 不合法")

    # 流尚未开始，可直接回标准 429，便于前端与监控区分「被限流」与「流内错误」
    rejected = await limiter.allow(user_id)
    if rejected is not None:
        raise ApiError(CODE_TOO_MANY_REQUESTS, _reject_message(rejected))

    # 会话注册是旁路：异步执行、脱离请求生命周期，客户端断开也能把标题写进去
    from core.tasktracker import tracker
    tracker().add(asyncio.create_task(_register_session(user_id, session_id, query),
                                      name="register_session"))

    return StreamingResponse(
        _sse_stream(user_id, session_id, query, limiter),
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关掉 nginx 缓冲，否则事件被攒批下发、失去实时性
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------- 会话历史


@router.get("/sessions")
async def list_sessions(user_id: CurrentUser):
    try:
        items = await agent_bridge.list_sessions(str(user_id))
    except Exception as exc:
        logger.info(
            "rid=%s agent sessions list failed: %s", current_request_id(), exc
        )
        raise ApiError(CODE_BAD_GATEWAY, "会话列表获取失败") from exc
    return ok_response({"sessions": items})


@router.post("/sessions")
async def create_session(request: Request, user_id: CurrentUser):
    payload = await read_json(request, 0, "请求体格式错误")
    session_id = _trim(payload.get("session_id"))
    if not _valid_session_id(session_id):
        raise ApiError(CODE_BAD_REQUEST, "session_id 不合法")
    title = _trim(payload.get("title"))
    if len(title) > 200:
        title = title[:200]

    try:
        await agent_bridge.upsert_session(str(user_id), session_id, title)
    except Exception as exc:
        logger.info(
            "rid=%s agent session create failed: %s", current_request_id(), exc
        )
        raise ApiError(CODE_BAD_GATEWAY, "会话创建失败") from exc
    return ok_response({"session_id": session_id})


@router.get("/sessions/{session_id}/messages")
async def session_messages(session_id: str, user_id: CurrentUser):
    if not _valid_session_id(session_id):
        raise ApiError(CODE_BAD_REQUEST, "session_id 不合法")
    try:
        items = await agent_bridge.session_messages(str(user_id), session_id)
    except Exception as exc:
        logger.info(
            "rid=%s agent session messages failed: %s", current_request_id(), exc
        )
        raise ApiError(CODE_BAD_GATEWAY, "会话消息获取失败") from exc
    return ok_response({"messages": items})


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user_id: CurrentUser):
    if not _valid_session_id(session_id):
        raise ApiError(CODE_BAD_REQUEST, "session_id 不合法")
    try:
        deleted = await agent_bridge.delete_session(str(user_id), session_id)
    except Exception as exc:
        logger.info(
            "rid=%s agent session delete failed: %s", current_request_id(), exc
        )
        raise ApiError(CODE_BAD_GATEWAY, "会话删除失败") from exc
    return ok_response({"deleted": deleted})
