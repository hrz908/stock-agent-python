"""护栏节点：轻量 prompt-injection 检测 + 每轮请求的状态边界。

只做规则匹配（不调 LLM），命中即写入 state["error"]，由主图条件边路由到降级节点，
可疑输入绝不进入后续链路。作为主图入口，它还负责把「本轮工具结果」清零，
并把 messages 裁到窗口内（_trim_history），避免 checkpoint 随轮数膨胀。
"""
import re
import time

from langchain_core.messages import ToolMessage
from langgraph.types import Overwrite

from agent.core.llm import estimate_tokens

# 常见注入/越权话术（中英），大小写不敏感
_INJECTION_PATTERNS = [
    r"忽略(以上|之前|前面)(的)?(所有)?(规则|指令|提示|设定)",
    r"无视(以上|之前|前面)(的)?(所有)?(规则|指令|提示|设定)",
    r"忘记(你(的|是)|之前)(的)?(身份|规则|指令|设定|提示)",
    r"你现在(是|不再是|扮演)",
    r"ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|rules?|prompts?)",
    r"disregard\s+(all\s+)?(previous|above|prior)",
    r"forget\s+(everything|your\s+instructions|the\s+rules)",
    r"you\s+are\s+now\s+(a|an|no\s+longer)",
    r"system\s*prompt",
    r"reveal\s+(your\s+)?(system|instructions|prompt)",
    r"new\s+instructions?:",
    r"developer\s+mode",
    r"jailbreak",
]
_REGEX = re.compile("|".join(f"(?:{p})" for p in _INJECTION_PATTERNS), re.IGNORECASE)

# 消息窗口：checkpoint 每轮只增不减，blob 随会话轮数线性膨胀（每个版本都存全量 messages）。
# 生成侧 recent_turns 窗口只有 6~10 条，这里裁到 40 条对上下文零影响，纯粹给存储瘦身。
_MSG_WINDOW = 40
# 窗口内再按 token 兜一道：超了继续从头部丢（防单条超大粘贴把窗口撑爆）
_MSG_TOKEN_BUDGET = 12000


def _msg_tokens(msg) -> int:
    c = getattr(msg, "content", "")
    return estimate_tokens(c if isinstance(c, str) else str(c))


def _trim_history(messages: list) -> list:
    """把 messages 裁到窗口内，返回可直接 Overwrite 的列表。

    两个约束：末尾必须是本轮新问题（最后一条 HumanMessage），只裁头部；
    裁完不能留下悬挂的 ToolMessage——其配对的 AIMessage(tool_calls) 已被裁掉，
    孤儿 ToolMessage 会让上游接口报错（口径同 state.recent_turns）。
    """
    out = list(messages or [])
    if len(out) > _MSG_WINDOW:
        out = out[-_MSG_WINDOW:]
    # 逐条算一次再从头丢：原实现在 while 里反复全量重算，
    # 长会话每轮都要多编码几千 token，纯 CPU 浪费
    costs = [_msg_tokens(m) for m in out]
    total = sum(costs)
    drop = 0
    while drop < len(out) - 2 and total > _MSG_TOKEN_BUDGET:
        total -= costs[drop]
        drop += 1
    out = out[drop:]
    while out and isinstance(out[0], ToolMessage):
        out = out[1:]
    return out


async def guardrail(state: dict) -> dict:
    """检测注入话术；同时把「本轮工具结果」清零、messages 裁到窗口内。

    tool_results 用 operator.add 跨工具轮次累加，但它会随 checkpointer 跨「用户轮次」一起留存：
    不清零的话 synthesize 会把历史轮次的卡片重发一遍（实测同 thread 第二轮卡片成倍）。
    每轮请求都从本节点开始，用 Overwrite 绕过 reducer 强制清零，保证 synthesize 只看到本轮取数结果；
    messages 同理，整体替换为裁剪后的窗口，阻止 checkpoint 膨胀。
    """
    t0 = time.monotonic()
    query = state.get("query", "") or ""
    trimmed = _trim_history(state.get("messages") or [])
    timings = {"guardrail_ms": int((time.monotonic() - t0) * 1000)}
    # cards 记录「本轮已发出的卡片类型」，用于同一轮重复合成时防止重复发卡；
    # 必须逐轮清零，否则上一轮发过的类型会把本轮同类卡片压掉
    if _REGEX.search(query):
        return {
            "error": "guardrail_blocked",
            "intent": "clarify",
            "confidence": 0.0,
            "tool_results": Overwrite([]),
            "cards": [],
            "messages": Overwrite(trimmed),
            # timings 走 merge reducer；每轮必须 Overwrite 重置，否则上一轮的
            # tool_call_ms 等会跨用户轮次累加，第二轮耗时看起来像 20s+。
            "timings": Overwrite(timings),
        }
    return {
        "error": None,
        "tool_results": Overwrite([]),
        "cards": [],
        "messages": Overwrite(trimmed),
        "timings": Overwrite(timings),
    }
