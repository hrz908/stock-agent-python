"""股票代码搜索工具（模糊联想：代码前缀 / 中文名 / 拼音缩写）。"""
from langchain_core.tools import tool

from agent.tools import call_tool_safe, go_get


@tool
async def symbol_search(query: str, limit: int = 5) -> dict:
    """按关键词模糊搜索股票，返回候选代码列表。

    Args:
        query: 搜索词，支持代码前缀、中文名、拼音缩写（如 "maotai"、"贵州茅台"、"600"）。
        limit: 返回条数，默认 5，最多 10。

    Returns:
        {"ok": True, "data": [{code, market, name}, ...]} 成功；
        {"ok": False, "error": "..."} 失败降级。
    """
    params = {"q": query, "limit": limit}
    return await call_tool_safe(go_get, "/api/v1/stocks/search", params)
