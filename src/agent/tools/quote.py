"""实时行情工具。"""
from langchain_core.tools import tool

from agent.tools import call_tool_safe, go_get


@tool
async def get_quote(code: str) -> dict:
    """获取指定股票的实时行情快照。

    Args:
        code: 股票代码，如 "600519" 或 "SH600519"。

    Returns:
        {"ok": True, "data": {...}} 成功；{"ok": False, "error": "..."} 失败（超时/异常降级）。
    """
    return await call_tool_safe(go_get, "/api/v1/stock/quote", {"code": code})
