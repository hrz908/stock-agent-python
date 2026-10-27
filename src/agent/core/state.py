"""图状态定义与共享工具。

AgentState 贯穿主图与子图，约定：
- messages 用 add_messages 累加（human / ai / tool）；
- tool_results 用 operator.add 跨轮追加，子图节点只返回「本轮新增」，避免手写 prev + 的脆弱逻辑；
- 其余字段（rewritten / intent / retrieved / cards / citations / entities 等）覆盖式写入。
所有节点只写自己负责的字段，避免相互踩踏。
"""
import operator
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph.message import add_messages


def merge_timings(left: dict, right: dict) -> dict:
    """阶段计时按 key 合并：节点只返回自己负责的耗时，后写覆盖同名 key。"""
    return {**(left or {}), **(right or {})}


class AgentState(TypedDict, total=False):
    # 对话消息（含 human / ai / tool），由 add_messages 累加
    messages: Annotated[list, add_messages]
    user_id: str
    thread_id: str
    # 当前轮用户原始输入
    query: str
    # 改写后的独立问题（指代消解结果）
    rewritten: str
    # 意图路由：stock / stock_agent / chat / off_topic / clarify；
    # stock_agent 可来自 classify 的实时数据快路径，也可来自 rag 未命中后的升级
    intent: str
    confidence: float
    # 分类节点抽取的实体（symbol / market / date_range），供下游复用
    entities: dict
    # 工具调用结果（stock 子图写入，operator.add 跨轮累加）
    tool_results: Annotated[list, operator.add]
    # RAG 检索片段
    retrieved: list
    # 结构化卡片事件（quote_card 等）
    cards: list
    # 引用来源
    citations: list
    # 错误信息：非空时条件边路由到降级节点
    error: str | None
    # token 用量统计
    usage: dict
    # 阶段耗时（毫秒）：rewrite_ms / classify_ms / embed_ms / sql_ms / rerank_ms /
    # judge_ms / tool_call_ms / tool_execute_ms / synth_ttft_ms / synth_ms 等
    timings: Annotated[dict, merge_timings]


def initial_state(user_id: str, thread_id: str, query: str) -> AgentState:
    """构造一轮请求的初始状态，给所有可变字段填安全默认值。"""
    return AgentState(
        messages=[],
        user_id=user_id,
        thread_id=thread_id,
        query=query,
        rewritten="",
        intent="",
        confidence=0.0,
        entities={},
        tool_results=[],
        retrieved=[],
        cards=[],
        citations=[],
        error=None,
        usage={},
        timings={},
    )


def recent_turns(state: dict, limit: int = 6, query: str = None) -> list:
    """取最近 limit 条「纯文本」对话轮次，供 LLM 上下文。

    统一剔除 ToolMessage（json 载荷进对话会触发上游 max_seq_len 报错）
    与带 tool_calls 的 AIMessage（残缺配对会让上游接口报错）；
    传入 query 时额外跳过内容等于当前问题的人类消息，避免调用方追加时重复。
    末尾的当前轮一并排除，保证上下文口径一致；stock / rag / chat 三个子图共用。
    """
    msgs = list(state.get("messages", []))
    body = msgs[:-1] if msgs else []  # 排除末尾当前轮
    q = (query or "").strip()
    out = []
    for m in body[-(limit + 1):]:
        if isinstance(m, ToolMessage):
            continue
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            continue
        if (
            q
            and isinstance(m, HumanMessage)
            and (m.content if isinstance(m.content, str) else str(m.content or "")).strip() == q
        ):
            continue
        out.append(m)
    return out[-limit:]
