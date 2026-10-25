"""会话注册表与 checkpoint 生命周期维护。

「历史对话」需要人类可读的会话列表（标题 + 最近活跃），而 LangGraph checkpointer 只有 thread_id、
没有标题，「新对话」又要在首条消息之前就出现在列表里——故由本模块维护 chat_sessions 注册表：
- register（「新对话」按钮）：立即插一行占位（auto_title=true，标题「新对话」）；
- upsert（每轮 run，由 Go 网关代理调用）：标题替换为首条真实问题，之后只更新 updated_at；
- list / delete：历史列表读取与删除（删除联动清 checkpoints 与 Redis 摘要）。

「checkpoint 无限增长」的三道闸在此闭环：guardrail 每轮把 messages 裁到窗口内（blob 有界）；
prune_thread_checkpoints 每轮 run 前删旧版本、补回最新一份（行数有界）；
本模块在 upsert 后淘汰单用户超过 SESSION_KEEP_MAX 的旧会话（删注册行 + adelete_thread + 摘要）。
"""
import logging
import re

from langchain_core.messages import AIMessage, HumanMessage

from config import SESSION_KEEP_MAX, SESSION_LIST_MAX
from core.pg import get_pool
from core.redis import get_redis

logger = logging.getLogger("stocka.agent")

# session_id 只允许安全字符：它进 thread_id（default:user:session），
# 放任特殊字符会污染 PG 参数与 Redis key
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    user_id     TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    auto_title  BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, session_id)
)
"""
_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS chat_sessions_user_updated "
    "ON chat_sessions (user_id, updated_at DESC)"
)

_ensured = False


def valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.match(session_id or ""))


def thread_id_of(user_id: str, session_id: str) -> str:
    """与 Go 网关 sse_agent.go 一致的 thread_id 拼装口径。"""
    return f"default:{user_id}:{session_id}"


async def ensure_sessions_table() -> None:
    """建表（幂等）：启动建一次，之后各入口只做一次布尔判断。"""
    global _ensured
    if _ensured:
        return
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(_TABLE_SQL)
        await conn.execute(_INDEX_SQL)
        await conn.commit()
    _ensured = True


async def _ensure() -> None:
    """懒建表：启动预热失败（DB 未就绪）时不至于让会话接口一直 500。"""
    global _ensured
    if not _ensured:
        try:
            await ensure_sessions_table()
        except Exception as e:  # noqa: BLE001 - 建表失败不缓存成功态，下轮再试
            logger.warning("ensure chat_sessions table failed: %s", e)


async def upsert_session(
    user_id: str, session_id: str, title: str, auto_title: bool = False
) -> None:
    """注册/刷新会话。auto_title=true 表示这是「新对话」占位行。

    标题口径：只有占位行（或空标题）会被真实提问替换，已有真实标题的只更新 updated_at，
    历史列表始终显示首次提问、不跟着最新一轮乱变。任何失败只记日志——注册是旁路，不能阻断对话。
    """
    try:
        await _ensure()
        title = (title or "").strip()[:40]
        if not title:
            title = "新对话"
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO chat_sessions (user_id, session_id, title, auto_title)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, session_id) DO UPDATE SET
                    updated_at = now(),
                    title = CASE
                        WHEN chat_sessions.auto_title OR chat_sessions.title = ''
                        THEN EXCLUDED.title ELSE chat_sessions.title END,
                    auto_title = false
                """,
                (user_id, session_id, title, auto_title),
            )
            await conn.commit()
        await _evict_old_sessions(user_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("upsert session failed (%s/%s): %s", user_id, session_id, e)


async def list_sessions(user_id: str, limit: int = SESSION_LIST_MAX) -> list:
    """按最近活跃倒序列出会话（不含消息体，切会话时按需拉取）。"""
    await _ensure()
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT session_id, title, updated_at
                FROM chat_sessions
                WHERE user_id = %s
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (user_id, limit),
            )
            rows = await cur.fetchall()
    return [
        {
            "session_id": r[0],
            "title": r[1] or "新对话",
            # ISO 字符串，前端可直接展示/排序
            "updated_at": r[2].isoformat() if r[2] else "",
        }
        for r in rows
    ]


async def session_messages(user_id: str, session_id: str) -> list:
    """从最新 checkpoint 取纯文本对话（切会话/恢复上下文用）。

    只保留 human / ai 纯文本：ToolMessage（JSON 载荷）与带 tool_calls 的 AIMessage
    对前端没有意义、还会破坏消息配对。结构化卡片不持久化（checkpoint 只有文本），
    恢复的会话以文本气泡呈现。
    """
    from core.checkpointer import get_checkpointer

    cp = await get_checkpointer()
    tn = await cp.aget_tuple(
        {"configurable": {"thread_id": thread_id_of(user_id, session_id), "checkpoint_ns": ""}}
    )
    if tn is None:
        return []
    values = tn.checkpoint.get("channel_values", {}) or {}
    out = []
    for m in values.get("messages", []) or []:
        content = getattr(m, "content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        if isinstance(m, HumanMessage):
            out.append({"role": "user", "content": content})
        elif isinstance(m, AIMessage) and not getattr(m, "tool_calls", None):
            out.append({"role": "assistant", "content": content})
    return out


async def delete_session(user_id: str, session_id: str) -> bool:
    """删除会话：注册行 + checkpoints + Redis 滚动摘要；返回是否删过。"""
    await _ensure()
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM chat_sessions WHERE user_id = %s AND session_id = %s",
                (user_id, session_id),
            )
            deleted = cur.rowcount or 0
        await conn.commit()
    await _drop_thread(thread_id_of(user_id, session_id))
    return deleted > 0


async def _drop_thread(thread_id: str) -> None:
    """清掉一个 thread 的 checkpoint 与滚动摘要，失败只记日志。"""
    try:
        from core.checkpointer import get_checkpointer

        cp = await get_checkpointer()
        if hasattr(cp, "adelete_thread"):
            await cp.adelete_thread(thread_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("adelete_thread failed (%s): %s", thread_id, e)
    try:
        await get_redis().delete(f"agent:summary:{thread_id}")
    except Exception:  # noqa: BLE001 - 摘要清理失败无关紧要
        pass


async def _evict_old_sessions(user_id: str) -> None:
    """单用户会话数超上限时按 updated_at 淘汰最旧的。

    没有这道闸，会话数本身也会无限增长。限额取足（默认 50），正常使用碰不到；
    碰到的场景是脚本 / 误用。
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT session_id FROM chat_sessions
                WHERE user_id = %s
                ORDER BY updated_at DESC
                OFFSET %s
                """,
                (user_id, SESSION_KEEP_MAX),
            )
            stale = [r[0] for r in await cur.fetchall()]
            if not stale:
                return
            for sid in stale:
                await cur.execute(
                    "DELETE FROM chat_sessions WHERE user_id = %s AND session_id = %s",
                    (user_id, sid),
                )
            await conn.commit()
    for sid in stale:
        logger.info("evict stale session: user=%s session=%s", user_id, sid)
        await _drop_thread(thread_id_of(user_id, session_id=sid))
