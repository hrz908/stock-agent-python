"""纯对话子图：单节点流式生成，无工具、无检索。

用 prompts/chat.xml 作 system、携带最近历史直接流式产出；
token 在 astream 循环里显式 emit("token", ...) 经 main.py 的 custom 模式透传，
不依赖 LangGraph 的 messages 流模式（避免双重发射）。
"""
from datetime import datetime

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from core.llm import get_llm
from core.state import AgentState, recent_turns
from core.template import render
from subgraphs import emit


async def llm_stream(state: dict) -> dict:
    query = state.get("rewritten") or state.get("query", "") or ""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    system = render("chat.xml", now=now, query=query)

    # 保留最近历史以维持连贯（复用 recent_turns 统一口径，跳过当前问题避免重复）
    history = recent_turns(state, 10, query=query)
    msgs = [SystemMessage(content=system)] + history

    llm = get_llm(streaming=True, temperature=0.7)
    # 流式生成：逐块 emit token（经 custom 模式透传），同时累加成完整 AIMessage 写回 state
    parts = []
    async for chunk in llm.astream(msgs):
        if isinstance(chunk.content, str) and chunk.content:
            emit("token", {"delta": chunk.content})
        parts.append(chunk)
    ai = parts[0] if parts else None
    for p in parts[1:]:
        ai = ai + p
    return {"messages": [ai if ai is not None else AIMessage(content="")]}


def build_chat_subgraph():
    g = StateGraph(AgentState)
    g.add_node("llm_stream", llm_stream)
    g.add_edge(START, "llm_stream")
    g.add_edge("llm_stream", END)
    # 单节点子图，无需 checkpoint
    return g.compile(checkpointer=False)
