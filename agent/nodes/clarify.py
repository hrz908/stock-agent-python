"""澄清 / 降级节点。

两种入口：intent == "clarify"（问题含糊或低置信度，反问补全信息）；
state["error"] 非空（如护栏拦截，给出安全降级回复，不进入任何工具/检索链路）。
统一产出一条 AIMessage，随后进入 post 收尾。
"""
import time

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from core.llm import get_small_llm
from subgraphs import emit

_BLOCK_REPLY = "抱歉，我无法处理该请求。如果你有股票行情或投研相关的问题，欢迎换个方式告诉我。"

_FALLBACK = "为了给你准确的答案，能补充一下具体是哪只股票、以及想看的时间范围或指标吗？"

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
    ttft_ms = None
    try:
        llm = get_small_llm(streaming=True, temperature=0.3)
        parts = []
        async for chunk in llm.astream([
            SystemMessage(content=_CLARIFY_SYSTEM),
            HumanMessage(content=f"用户问题：{query}"),
        ]):
            if isinstance(chunk.content, str) and chunk.content:
                if ttft_ms is None:
                    ttft_ms = int((time.monotonic() - t0) * 1000)
                emit("token", {"delta": chunk.content})
            parts.append(chunk)
        text = "".join(
            (p.content if isinstance(p.content, str) else "") for p in parts
        ).strip()
    except Exception:  # noqa: BLE001 - 模型异常时用固定兜底话术
        text = ""
    if not text:
        # 模型没生成出文字：兜底话术同样要显式 emit，否则前端空白
        emit("token", {"delta": _FALLBACK})
    total_ms = int((time.monotonic() - t0) * 1000)
    return {
        "messages": [AIMessage(content=text or _FALLBACK)],
        "timings": {
            "clarify_ttft_ms": ttft_ms if ttft_ms is not None else total_ms,
            "clarify_ms": total_ms,
        },
    }
