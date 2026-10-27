"""stock 子图共享状态：工具集 + 绑定 LLM 的惰性初始化。

这是拆分后被其他 stock/ 子模块 import 的唯一"状态源头"。agent_bridge.shutdown 通过
tools.close_http_client / rag.reranker.close_rerank_client 收尾，这里不负责。
"""
from __future__ import annotations

import logging

from agent.config import TOOL_CALL_LLM_TIMEOUT
from agent.core.llm import get_llm
from agent.core.plan import fast_tool_calls
from agent.tools import get_tools, tool_cache_get, tool_cache_put

logger = logging.getLogger("stocka.agent")

# 取数类工具：只有它们的结果才能支撑回答里的数字。
# K 线/资讯/财务指标/龙虎榜/同业对比同属此类；不进白名单的话，
# 用户问「最近日K/新闻/ROE」即使取数成功也会被兜底文案覆盖。
DATA_TOOLS = frozenset({
    "get_quote", "get_valuation", "get_financial_report", "get_money_flow",
    "get_history_daily", "get_stock_news", "get_financial_indicator",
    "get_top_list", "get_peer_compare",
})

# ========== 进程级单例 ==========

# 工具集进程级缓存：首轮 tool_call 前经 _ensure_tools 懒加载
_TOOLS = None
# 绑定工具后的模型（进程级缓存）：bind_tools 每次都要把 13 个工具的 schema 转一遍
_bound_llm = None

# 工具名 -> 中文标签（tool_start 事件用，前端展示「正在获取 xx」）
_TOOL_LABELS = {
    "get_quote": "实时行情",
    "symbol_search": "股票搜索",
    "get_industry_peers": "同行业公司",
    "get_valuation": "估值指标",
    "get_financial_indicator": "财务指标",
    "get_financial_report": "财报数据",
    "get_money_flow": "资金流向",
    "get_stock_news": "财经资讯",
    "get_history_daily": "历史行情",
    "get_top_list": "龙虎榜席位",
    "get_peer_compare": "同业对比",
    "get_research_reports": "机构研报",
    "search_knowledge_base": "知识库检索",
}


async def _ensure_tools() -> None:
    """懒加载工具集：进程内 @tool 直连 + 知识库检索工具。"""
    global _TOOLS
    if _TOOLS is not None:
        return
    from agent.tools.rag_tool import search_knowledge_base

    tools = list(get_tools())
    try:
        tools.append(search_knowledge_base)
    except Exception:  # noqa: BLE001 - 检索工具构造失败不阻断取数工具
        pass
    _TOOLS = tools


async def warm_tools() -> int:
    """启动时预热工具集，返回工具数量；失败返回 0 由首请求重试。"""
    try:
        await _ensure_tools()
        return len(_tools())
    except Exception:  # noqa: BLE001 - 预热失败不影响启动
        return 0


def _tools() -> list:
    """同步取当前工具集；_ensure_tools 尚未跑过时退回直连工具。"""
    if _TOOLS is None:
        return get_tools()
    return _TOOLS


def _tools_by_name() -> dict:
    return {t.name: t for t in _tools()}


def _tool_bound_llm():
    global _bound_llm
    if _bound_llm is None:
        _bound_llm = get_llm(
            streaming=False,
            temperature=0.2,
            timeout=TOOL_CALL_LLM_TIMEOUT,
        ).bind_tools(_tools())
    return _bound_llm


__all__ = [
    "DATA_TOOLS",
    "_ensure_tools", "warm_tools", "_tools", "_tools_by_name", "_tool_bound_llm",
    "_TOOL_LABELS", "tool_cache_get", "tool_cache_put", "fast_tool_calls",
]
