"""主图组装。

START -> guardrail -> [error? clarify : supervisor -> route_by_intent]
route_by_intent -> {stock, stock_agent, chat, off_topic, clarify}
  - stock     -> rag 漏斗子图
                 命中 -> post
                 未命中 -> (intent 改写 stock_agent) -> stock_agent 子图 -> post
  - chat      -> post
  - off_topic -> refuse -> post
  - clarify   -> post
post -> END

route_by_intent 是纯函数，仅读取 state["intent"]，异常/未知一律回退 clarify。

supervisor（nodes/supervisor.py）一次小模型调用同时产出改写与意图，替代原来的
rewrite → classify 两跳；上下文三层（最近轮次 / 滚动摘要 / 上一轮结构化 ctx）都在
那一个节点里拼好，下游子图只认 rewritten 与 intent。

意图口径：
- stock      : 文档型股票问题，统一先进 RAG 漏斗，命中带引用直接作答；
- stock_agent: 内部意图，三个来源：
               1) supervisor 命中实时数据词，直接进取数子图（跳过 RAG）；
               2) rag 阈值 / 判读未命中，升级到取数子图；
               3) rag 覆盖闸命中且问的是「当下状态」，降级取数（rag.py）；
- chat       : 闲聊，直接陪聊，不挂工具；
- off_topic  : 与股票无关的正经话题，固定话术婉拒（nodes/refuse.py）；
- clarify    : 含糊 / 低置信度 / 护栏拦截，反问或安全降级。
"""
import asyncio
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from agent.core.keywords import (
    is_concept_question,
    is_consult_question,
    is_structured_data_question,
)
from agent.core.state import AgentState
from agent.nodes import clarify, guardrail, post, refuse, supervisor
from agent.subgraphs.chat import build_chat_subgraph
from agent.subgraphs.rag import build_rag_subgraph
from agent.subgraphs.stock import build_stock_subgraph

# classify 可产出的意图值：stock_agent 既可由 classify 的实时数据快路径写入，
# 也可由 rag 两级判定未命中时写入。
_CLASSIFY_INTENTS = {'stock', 'stock_agent', 'chat', 'off_topic', 'clarify'}


def route_after_guardrail(state: dict) -> Literal['clarify', 'supervisor']:
    """护栏命中 -> 降级澄清；否则交给 supervisor 做改写与路由。"""
    return 'clarify' if state.get('error') else 'supervisor'


def route_by_intent(
    state: dict,
) -> Literal['stock', 'stock_agent', 'chat', 'off_topic', 'clarify']:
    """纯函数路由：按 intent 分派；未知一律回退 clarify。

    结构化财务数据问句（main financial indicators / 营收增长情况 / 同比）即使
    classify 判成文档型 stock，也改路由到取数子图：这类问题需要工具数值与财报卡，
    语料扩充后走 RAG 会绕过结构化结果（实测 F-01）。概念问题不受影响。
    """
    intent = state.get('intent', '')
    if intent not in _CLASSIFY_INTENTS:
        return 'clarify'
    if intent == 'stock':
        text = state.get('rewritten') or state.get('query', '') or ''
        if is_structured_data_question(text) and not is_concept_question(text):
            return 'stock_agent'
        # 工具 / 入门咨询（"新手该用哪个炒股软件"）：语料库没有这类文档，进 RAG 只会
        # 白跑一次检索再降级。判据保守，带代码或要标的的问句不受影响。
        if is_consult_question(text):
            return 'chat'
    return intent


def route_after_rag(state: dict) -> Literal['stock_agent', 'chat', 'post']:
    """RAG 未命中会把 intent 改成 stock_agent（取数）或 chat（概念通用回答）。

    chat 分支只用于「概念/投教问题且知识库没有可用片段」：这类问题取数没有意义，
    交给通用问答直接解释，比升级到取数 agent 空跑 tool_call（8~16s）快一个量级。
    """
    intent = state.get('intent')
    if intent == 'stock_agent':
        return 'stock_agent'
    if intent == 'chat':
        return 'chat'
    return 'post'


def build_graph(checkpointer=None):
    g = StateGraph(AgentState)

    # 小模型节点可安全重试：它们不产生 token 事件，重试不会造成重复输出，
    # 429 / 连接抖动这类瞬时错误交给框架退避比在节点里手写 try 更省事
    small_retry = RetryPolicy(max_attempts=2, initial_interval=0.4, backoff_factor=2.0)

    # 节点
    g.add_node('guardrail', guardrail)
    # 改写与路由合并成一次小模型调用（原 rewrite -> classify 两跳）
    g.add_node('supervisor', supervisor, retry_policy=small_retry)
    g.add_node('clarify', clarify)
    g.add_node('refuse', refuse)
    g.add_node('post', post)
    g.add_node('stock_agent', build_stock_subgraph())
    g.add_node('rag', build_rag_subgraph())
    g.add_node('chat', build_chat_subgraph())

    # 边
    g.add_edge(START, 'guardrail')
    g.add_conditional_edges('guardrail', route_after_guardrail, {
        'clarify': 'clarify',
        'supervisor': 'supervisor',
    })
    g.add_conditional_edges('supervisor', route_by_intent, {
        'stock': 'rag',
        'stock_agent': 'stock_agent',
        'chat': 'chat',
        'off_topic': 'refuse',
        'clarify': 'clarify',
    })
    g.add_edge('stock_agent', 'post')
    g.add_conditional_edges('rag', route_after_rag, {
        'stock_agent': 'stock_agent',
        'chat': 'chat',
        'post': 'post',
    })
    g.add_edge('chat', 'post')
    g.add_edge('refuse', 'post')
    g.add_edge('clarify', 'post')
    g.add_edge('post', END)

    return g.compile(checkpointer=checkpointer)


_graph = None
_graph_lock = asyncio.Lock()


async def get_graph():
    """惰性构建带 checkpointer 的全局图单例（并发首个请求只构建一次）。"""
    global _graph
    if _graph is not None:
        return _graph
    async with _graph_lock:
        if _graph is None:
            from agent.core.checkpointer import get_checkpointer

            cp = await get_checkpointer()
            _graph = build_graph(cp)
    return _graph
