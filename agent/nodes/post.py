"""收尾节点：滚动摘要、免责声明、用量统计；另提供 agent_runs 审计落库能力。

在主图末尾（子图之后、END 之前）执行：
1. messages token 超阈值 → 小模型滚动摘要，写入 Redis（按 thread_id）；
2. 涉及个股判断 → 在最后一条 assistant 消息末尾追加免责声明（按 id 覆盖更新）；
3. 统计 usage 写回 state。
log_agent_run 供 main.py 在 done 事件前调用，把一轮运行写入 agent_runs 审计表。
所有步骤容错，任何一步失败都不阻断响应。
"""
import logging

from langchain_core.messages import AIMessage, HumanMessage

from config import SUMMARY_TOKEN_LIMIT
from core.llm import count_message_tokens, get_small_llm, message_chars
from core.pg import get_pool
from core.redis import get_redis

logger = logging.getLogger("stocka.agent")

_DISCLAIMER = "\n\n> 以上信息仅供参考，不构成任何投资建议。市场有风险，投资需谨慎。"

# 个股相关意图：这些回答需要挂免责声明。
# stock = 经 RAG 漏斗直接作答；stock_agent = 漏斗未命中后由炒股 agent 取数作答
_STOCK_INTENTS = {"stock", "stock_agent"}

def _collect_usage(messages: list) -> dict:
    """从消息的 usage_metadata 聚合 token 用量。"""
    total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for m in messages:
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
            await get_redis().set(
                f"agent:summary:{state.get('thread_id','')}", summary, ex=7 * 24 * 3600
            )
    except Exception:  # noqa: BLE001
        pass


async def log_agent_run(
    user_id,
    thread_id: str,
    intent: str = None,
    confidence: float = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    ttft_ms: int = None,
    total_ms: int = None,
    tool_calls: int = 0,
    error_code: str = None,
) -> None:
    """写一行 agent_runs 审计记录；失败只记日志，绝不阻断响应流。"""
    # user_id 列是 BIGINT NOT NULL：非数字身份（调试请求）直接跳过，
    # 否则每次插入都会因约束失败，审计行静默丢失
    if user_id is None:
        logger.debug("skip agent_runs insert: non-numeric user_id")
        return
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO agent_runs (user_id, thread_id, intent, confidence,
                                        tokens_in, tokens_out, ttft_ms, total_ms,
                                        tool_calls, error_code)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (user_id, thread_id, intent, confidence,
                 tokens_in or 0, tokens_out or 0, ttft_ms, total_ms,
                 tool_calls or 0, error_code),
            )
            await conn.commit()
    except Exception as e:  # noqa: BLE001 - 审计失败不影响主流程
        logger.warning("log_agent_run failed: %s", e)


async def post(state: dict) -> dict:
    # 1. 滚动摘要（后台容错）
    await _roll_summary(state)

    messages = list(state.get("messages", []))
    updates: dict = {"usage": _collect_usage(messages)}

    # 2. 免责声明：涉及个股时追加到最后一条 assistant 消息（按 id 覆盖）
    intent = state.get("intent", "")
    if intent in _STOCK_INTENTS:
        for m in reversed(messages):
            if isinstance(m, AIMessage):
                content = m.content if isinstance(m.content, str) else str(m.content)
                if content.strip() and _DISCLAIMER.strip() not in content:
                    updates["messages"] = [AIMessage(id=m.id, content=content + _DISCLAIMER)]
                break

    return updates
