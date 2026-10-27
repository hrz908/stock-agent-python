"""对话相关业务：历史会话注册与查询。

LLM 的实际生成在 agent/ 里，这一层只做「网关视角的会话管理」：
把 agent 的异常翻译成对外错误码，把 session_id 的合法性校验收敛到一处。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gateway.adapters import agent_bridge
from gateway.infra.context import current_request_id
from gateway.infra.resp import CODE_BAD_GATEWAY, ApiError

logger = logging.getLogger("stocka.gateway.chat")

MAX_SESSION_ID_LEN = 64
SESSION_TITLE_RUNES = 40


def trim(text: Any) -> str:
    return text.strip() if isinstance(text, str) else ""


def valid_session_id(session_id: str) -> bool:
    """session_id 会拼进 thread_id 与 Redis key，只放行安全字符。

    与 agent 侧 valid_session_id 同口径，避免两边判定不一致导致 400/500 摇摆。
    """
    text = session_id or ""
    if not text or len(text) > MAX_SESSION_ID_LEN:
        return False
    return all(ch.isascii() and (ch.isalnum() or ch in ("_", "-")) for ch in text)


async def register_session(user_id: int, session_id: str, query: str) -> None:
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


def spawn_register_session(user_id: int, session_id: str, query: str) -> None:
    """脱离请求生命周期地注册会话：客户端断开也能把标题写进去。"""
    agent_bridge.track_task(
        register_session(user_id, session_id, query), name="register_session"
    )


async def list_sessions(user_id: int) -> list[dict[str, Any]]:
    try:
        return await agent_bridge.list_sessions(str(user_id))
    except Exception as exc:
        logger.info("rid=%s agent sessions list failed: %s", current_request_id(), exc)
        raise ApiError(CODE_BAD_GATEWAY, "会话列表获取失败") from exc


async def create_session(user_id: int, session_id: str, title: str) -> None:
    try:
        await agent_bridge.upsert_session(str(user_id), session_id, title)
    except Exception as exc:
        logger.info("rid=%s agent session create failed: %s", current_request_id(), exc)
        raise ApiError(CODE_BAD_GATEWAY, "会话创建失败") from exc


async def session_messages(user_id: int, session_id: str) -> list[dict[str, Any]]:
    try:
        return await agent_bridge.session_messages(str(user_id), session_id)
    except Exception as exc:
        logger.info("rid=%s agent session messages failed: %s", current_request_id(), exc)
        raise ApiError(CODE_BAD_GATEWAY, "会话消息获取失败") from exc


async def delete_session(user_id: int, session_id: str) -> bool:
    try:
        return await agent_bridge.delete_session(str(user_id), session_id)
    except Exception as exc:
        logger.info("rid=%s agent session delete failed: %s", current_request_id(), exc)
        raise ApiError(CODE_BAD_GATEWAY, "会话删除失败") from exc
