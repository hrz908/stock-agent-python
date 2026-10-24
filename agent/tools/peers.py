"""同行业候选工具（数据源：stock_basic / stock_industry 主数据表）。

存在理由：get_peer_compare 的 peers 以前靠模型「按行业常识给代码」，
给错代码会让对比卡里出现完全不相干的公司；这里用主数据的行业分类查同业
（东财优先，东财不可用时是新浪分类，见 ingest/ashare/master.py 的主备源）。

本工具不产任何行情数字，因此**不进** stock 子图的 _DATA_TOOLS：
进了会让「没取到数据」的兜底误判为已取数，模型反而可能凭记忆编数字。
"""
from langchain_core.tools import tool

from tools import call_tool_safe
from tools.market import digits

# 一次最多给多少候选：够模型挑 2 家，又不至于把上下文铺满（仓储侧同样收紧到 20）
_MAX_PEERS = 20
_DEFAULT_TOP_N = 6


def _top_n(v) -> int:
    """top_n 收敛到 [1, _MAX_PEERS]；0 / None / 非数字都别变成异常抛到工具外面。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = _DEFAULT_TOP_N
    return max(1, min(n, _MAX_PEERS))


@tool
async def get_industry_peers(code: str, top_n: int = 6) -> dict:
    """查询同行业公司（东财行业分类），用于不知道同行代码时挑选对比标的。
    只返回代码与名称，不含任何行情数字；要对比请再用 get_peer_compare。

    Args:
        code: 股票代码，如 "600519" 或 "SH600519"。
        top_n: 返回候选数，默认 6，最多 20。

    Returns:
        {"ok": True, "data": {code, name, market, industry, industry_detail,
        peers: [{code, name, market}]}}；
        peers 在同一级行业内按「同三级 > 同二级 > 同大类」排序（白酒的同行优先于饮料）；
        主数据未同步或数据库不可用时 {"ok": False, "error": ...}，不影响其余工具。
    """
    async def _do() -> dict:
        c = digits(code)
        if not c:
            return {"ok": False, "error": "股票代码无效"}
        from repo.stock import industry_peers

        return await industry_peers(c, limit=_top_n(top_n))

    return await call_tool_safe(_do, timeout=5.0)
