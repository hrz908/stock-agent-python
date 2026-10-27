"""off_topic 婉拒节点。

问题正经但与股票无关（写作业、菜谱、其他领域咨询）——不进工具也不进检索，
按固定话术婉拒并给出引导。固定文案而非 LLM 生成：口径稳定、零成本、零幻觉。
与 clarify（反问澄清）、guardrail 降级是三条不同的降级链路，互不复用。
"""
import asyncio
import time

from langchain_core.messages import AIMessage

from agent.subgraphs import emit

_REFUSE_TEXT = (
    "我是股票分析助手「溜来」，只擅长股票行情、财报、研报和投资知识相关的问题。"
    "刚才的话题超出了我的能力范围，换个股票问题问我吧，"
    "比如「贵州茅台最新行情」或「最近哪些股票上了龙虎榜」。"
)

# 拆成小块按打字机节奏发，前端呈现自然的流式效果
_CHUNK = 16
_CHUNK_INTERVAL = 0.02


async def refuse(state: dict) -> dict:
    t0 = time.monotonic()
    for i in range(0, len(_REFUSE_TEXT), _CHUNK):
        emit("token", {"delta": _REFUSE_TEXT[i:i + _CHUNK]})
        await asyncio.sleep(_CHUNK_INTERVAL)
    return {
        "messages": [AIMessage(content=_REFUSE_TEXT)],
        "timings": {"refuse_ms": int((time.monotonic() - t0) * 1000)},
    }
