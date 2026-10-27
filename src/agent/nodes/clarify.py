"""澄清 / 降级节点。

两种入口：intent == "clarify"（问题含糊或低置信度，反问补全信息）；
state["error"] 非空（如护栏拦截，给出安全降级回复，不进入任何工具/检索链路）。
统一产出一条 AIMessage，随后进入 post 收尾。
"""
import time

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.core.llm import get_small_llm
from agent.subgraphs import emit

_BLOCK_REPLY = "抱歉，我无法处理该请求。如果你有股票行情或投研相关的问题，欢迎换个方式告诉我。"

_FALLBACK = "为了给你准确的答案，能补充一下具体是哪只股票、以及想看的时间范围或指标吗？"

# 模型输出的「反问要素」：命中任一即认为澄清语义成立；全不命中时退固定话术，
# 避免措辞波动把「反问补全信息」写成陈述句（实测 R-03 偶发不含疑问词）
_CLARIFY_MARKERS = ("哪", "什么", "请", "具体", "补充")

_CLARIFY_SYSTEM = (
    "你是股票投研助手。用户的问题信息不足，需要反问澄清。"
    "用一句友好的中文，指出缺少哪些关键信息（如具体标的、时间范围、指标口径），"
    "并给出 1-2 个可直接点击的示例问法。不要编造数据，不要冗长。"
)


async def clarify(state: dict) -> dict:
    t0 = time.monotonic()
    # 护栏拦截：直接安全降级，不调模型。固定文案必须 emit 成 token——
    # main.py 只透传 custom 事件，光返回 AIMessage 前端拿不到任何文字
    if state.get("error") == "guardrail_blocked":
        emit("token", {"delta": _BLOCK_REPLY})
        return {
            "messages": [AIMessage(content=_BLOCK_REPLY)],
            "timings": {"clarify_ms": int((time.monotonic() - t0) * 1000)},
        }

    query = state.get("rewritten") or state.get("query", "") or ""
    try:
        # 非流式：先拿到完整文案再判「是否真的在反问」，不合格就整条换成固定话术，
        # 避免流式已吐出的陈述句无法撤回
        llm = get_small_llm(streaming=False, temperature=0.3)
        resp = await llm.ainvoke([
            SystemMessage(content=_CLARIFY_SYSTEM),
            HumanMessage(content=f"用户问题：{query}"),
        ])
        text = resp.content.strip() if isinstance(resp.content, str) else ""
    except Exception:  # noqa: BLE001 - 模型异常时用固定兜底话术
        text = ""
    if not text or not any(m in text for m in _CLARIFY_MARKERS):
        # 模型没生成出文字或没问到点子上：兜底话术同样要显式 emit，否则前端空白
        text = _FALLBACK
    emit("token", {"delta": text})
    total_ms = int((time.monotonic() - t0) * 1000)
    return {
        "messages": [AIMessage(content=text)],
        "timings": {
            "clarify_ttft_ms": total_ms,
            "clarify_ms": total_ms,
        },
    }
