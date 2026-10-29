"""收尾节点：滚动摘要、结构化上下文、用量统计；另提供 agent_runs 审计落库能力。

在主图末尾（子图之后、END 之前）执行：
1. messages token 超阈值 → 小模型滚动摘要，写入 Redis（按 thread_id）；
2. 本轮标的与链路写 agent:ctx:{thread_id}，供下一轮 supervisor 消解指代；
3. 统计 usage 写回 state。
log_agent_run 供 main.py 在 done 事件之后调用（后台落库），把一轮运行写入 agent_runs 审计表。
所有步骤容错，任何一步失败都不阻断响应。
"""
import asyncio
import logging
import time

from langchain_core.messages import AIMessage, HumanMessage
from psycopg.types.json import Jsonb

from agent.config import SUMMARY_TOKEN_LIMIT
from agent.core.llm import count_message_tokens, get_small_llm, message_chars
from agent.core.redis import get_redis
from agent.core.tasktracker import tracker
from agent.rag.entity import entity_mentions
from agent.storage.app import get_app_pool

logger = logging.getLogger("stocka.agent")

# 距上次摘要至少再攒这么多条消息才重算。会话一旦越过 token 阈值，messages 会被
# guardrail 裁在 12k 内长期停留，若每轮都重算，摘要就变成「每轮一次小模型调用」，
# 而两三轮内新增的内容对摘要价值很低。
_SUMMARY_MIN_NEW_MSGS = 4


def _spawn_summary(state: dict) -> None:
    """把滚动摘要移出关键路径：post 在流末尾，等一次小模型调用会把 done 事件推后。"""
    tracker().add(asyncio.create_task(_roll_summary(state), name="roll_summary"))


def _collect_usage(messages: list) -> dict:
    """聚合「本轮」消息的 token 用量。

    messages 带 checkpointer、跨轮累积：把整条会话加起来会让审计里的 tokens_*
    随会话变长单调膨胀（线上实测出现过单轮 500 万 output tokens）。
    因此从最后一条 HumanMessage 起算，只统计本轮——工具轮与作答轮都算在内。
    """
    start = 0
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            start = i
            break
    total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for m in messages[start:]:
        um = getattr(m, "usage_metadata", None)
        if isinstance(um, dict):
            total["input_tokens"] += um.get("input_tokens", 0) or 0
            total["output_tokens"] += um.get("output_tokens", 0) or 0
            total["total_tokens"] += um.get("total_tokens", 0) or 0
    return total


async def _roll_summary(state: dict) -> None:
    """超阈值时做滚动摘要写 Redis；失败静默。"""
    messages = state.get("messages", [])
    # 先用字符数做廉价下界：短会话完全不必惊动 tiktoken
    if message_chars(messages) < SUMMARY_TOKEN_LIMIT * 0.3:
        return
    if count_message_tokens(messages) <= SUMMARY_TOKEN_LIMIT:
        return
    # 频次闸：距上次摘要新增消息太少就跳过（阈值越过之后 messages 会长期停在
    # 12k 窗口内，没有这道闸等于每轮都调一次小模型）
    tid = state.get("thread_id", "")
    try:
        prev = await get_redis().get(f"agent:summary_msgs:{tid}")
        if prev and len(messages) - int(prev) < _SUMMARY_MIN_NEW_MSGS:
            return
    except Exception:  # noqa: BLE001 - 频次闸不可用不影响摘要本身
        pass
    lines = []
    for m in messages:
        if isinstance(m, HumanMessage):
            lines.append(f"用户: {m.content}")
        elif isinstance(m, AIMessage):
            c = m.content if isinstance(m.content, str) else str(m.content)
            if c.strip():
                lines.append(f"助手: {c}")
    if not lines:
        return
    try:
        llm = get_small_llm(streaming=False, temperature=0.0)
        prompt = (
            "请把以下对话压缩成简洁的中文摘要，保留关键实体、结论与用户偏好：\n"
            + "\n".join(lines)
        )
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        summary = (resp.content or "").strip()
        if summary:
            redis = get_redis()
            await redis.set(f"agent:summary:{tid}", summary, ex=7 * 24 * 3600)
            await redis.set(f"agent:summary_msgs:{tid}", len(messages), ex=7 * 24 * 3600)
    except Exception:  # noqa: BLE001
        pass


_CTX_TTL = 7 * 24 * 3600


async def _remember_context(state: dict) -> None:
    """把本轮标的与链路记进 Redis，供下一轮 supervisor 消解指代。

    行情卡走 custom 事件、不进 messages：只靠对话历史时，卡片轮次之后的「它现在什么价」
    找不到上文。这里单独留一份结构化结果（标的 / 本轮问题 / 走的子图），TTL 与滚动摘要
    同为 7 天。取不到标的就不写——空上下文比错的上下文更糟。
    """
    thread_id = state.get("thread_id", "")
    if not thread_id:
        return
    text = state.get("rewritten") or state.get("query") or ""
    codes: list[str] = []
    try:
        codes = await entity_mentions(text)
    except Exception:  # noqa: BLE001 - 实体识别不可用只是少一份上下文
        codes = []
    if not codes:
        # 改写里没有的，回答正文里通常点明了（stock.xml 规则 9 要求点明标的）
        last_ai = next(
            (m for m in reversed(state.get("messages") or []) if isinstance(m, AIMessage)),
            None,
        )
        if last_ai is not None:
            content = last_ai.content if isinstance(last_ai.content, str) else ""
            try:
                codes = await entity_mentions((content or "")[:200])
            except Exception:  # noqa: BLE001
                codes = []
    if not codes:
        return
    try:
        redis = get_redis()
        key = f"agent:ctx:{thread_id}"
        await redis.hset(key, mapping={
            "symbols": ",".join(codes[:3]),
            "rewritten": text[:80],
            "subgraph": state.get("intent", "") or "",
        })
        await redis.expire(key, _CTX_TTL)
    except Exception as exc:  # noqa: BLE001 - 上下文写失败不影响本轮响应
        logger.info("remember context failed: %s", exc)


async def log_agent_run(
    user_id,
    thread_id: str,
    intent: str = None,
    confidence: float = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    ttft_ms: int = None,
    first_event_ms: int = None,
    total_ms: int = None,
    tool_calls: int = 0,
    error_code: str = None,
    timings: dict = None,
) -> None:
    """写一行 agent_runs 审计记录；失败只记日志，绝不阻断响应流。"""
    # user_id 列是 BIGINT NOT NULL：非数字身份（调试请求）直接跳过，
    # 否则每次插入都会因约束失败，审计行静默丢失
    if user_id is None:
        logger.debug("skip agent_runs insert: non-numeric user_id")
        return
    try:
        pool = await get_app_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO agent_runs (user_id, thread_id, intent, confidence,
                                        tokens_in, tokens_out, ttft_ms, first_event_ms,
                                        total_ms, tool_calls, error_code, timings)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (user_id, thread_id, intent, confidence,
                 tokens_in or 0, tokens_out or 0, ttft_ms, first_event_ms,
                 total_ms, tool_calls or 0, error_code, Jsonb(timings or {})),
            )
            await conn.commit()
    except Exception as e:  # noqa: BLE001 - 审计失败不影响主流程
        logger.warning("log_agent_run failed: %s", e)


async def post(state: dict) -> dict:
    t0 = time.monotonic()
    # 1. 滚动摘要放后台：它只影响下一轮的上下文，不该占本轮 done 事件之前的时间
    _spawn_summary(state)
    # 2. 结构化上下文必须在本轮结束前写完：下一轮 supervisor 立刻就要读
    await _remember_context(state)

    messages = list(state.get("messages", []))
    # 2. 统计用量。免责声明已按产品要求整条移除：正文不再追加任何声明块
    return {
        "usage": _collect_usage(messages),
        "timings": {"post_ms": int((time.monotonic() - t0) * 1000)},
    }
