"""纯对话子图：单节点流式生成，无工具、无检索。

用 prompts/chat.xml 作 system、携带最近历史直接流式产出；
token 在 astream 循环里显式 emit("token", ...) 经 main.py 的 custom 模式透传，
不依赖 LangGraph 的 messages 流模式（避免双重发射）。
"""
import time
from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from config import ANSWER_ENABLE_THINKING
from core.llm import get_llm
from core.sanitize import StreamCleaner
from core.state import AgentState, recent_turns
from core.template import render
from subgraphs import emit


async def llm_stream(state: dict) -> dict:
    t0 = time.monotonic()
    query = state.get("rewritten") or state.get("query", "") or ""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    system = render("chat.xml", now=now, query=query)

    # 历史在前、本轮问题作为真正的用户消息压在末尾。
    # 问句绝不能只写在 system 提示里：单轮会话没有 HumanMessage 时，模型会把
    # system 当成待补全的文档，直接吐出 <user_query>…</user_query>（线上实测），
    # 用户看到的就是「AI 把问题当成了回答」。
    history = recent_turns(state, 10, query=query)
    msgs = [SystemMessage(content=system)] + history + [HumanMessage(content=query)]

    llm = get_llm(streaming=True, temperature=0.7, thinking=ANSWER_ENABLE_THINKING)
    # 流式生成：逐块 emit token（经 custom 模式透传），同时累加成完整 AIMessage 写回 state。
    # 漏出的提示词标签在 cleaner 里剥掉，正文原样透传
    cleaner = StreamCleaner()
    parts = []
    sent = []
    ttft_ms = None
    last_usage = None
    async for chunk in llm.astream(msgs):
        if isinstance(chunk.content, str) and chunk.content:
            delta = cleaner.feed(chunk.content)
            if delta:
                if ttft_ms is None:
                    ttft_ms = int((time.monotonic() - t0) * 1000)
                sent.append(delta)
                emit("token", {"delta": delta})
        # 用量只认最后一块的累计值：chunk 相加会把 usage 反复累加，
        # 审计里曾出现单轮 500 万 output tokens 这种不可能的数字
        um = getattr(chunk, "usage_metadata", None)
        if um:
            last_usage = um
        parts.append(chunk)
    tail = cleaner.flush()
    if tail:
        sent.append(tail)
        emit("token", {"delta": tail})
    ai = parts[0] if parts else None
    for p in parts[1:]:
        ai = ai + p
    # 落库/AIMessage 里也只留净化后的正文：否则下一轮历史又把这堆标签喂回模型
    if ai is not None:
        patch = {"content": "".join(sent)}
        if last_usage:
            patch["usage_metadata"] = last_usage
        ai = ai.model_copy(update=patch)
    total_ms = int((time.monotonic() - t0) * 1000)
    return {
        "messages": [ai if ai is not None else AIMessage(content="")],
        "timings": {
            "chat_ttft_ms": ttft_ms if ttft_ms is not None else total_ms,
            "chat_generate_ms": total_ms,
        },
    }


def build_chat_subgraph():
    g = StateGraph(AgentState)
    g.add_node("llm_stream", llm_stream)
    g.add_edge(START, "llm_stream")
    g.add_edge("llm_stream", END)
    # 单节点子图，无需 checkpoint
    return g.compile(checkpointer=False)
