"""个股资讯工具（数据源：akshare 东财 stock_news_em）。

相比原 Tushare news（市场级快讯，需自行按名称过滤），东财接口天然按个股返回，
「公司动态只取当日信息」的产品约定落在本地时间过滤上：
发布时间早于今天 00:00 的一律剔除。
"""
from datetime import datetime

from langchain_core.tools import tool

from agent.tools import call_tool_safe
from agent.tools.market import digits, run_ds, text

# 资讯一次最多回多少条
_MAX_NEWS_ROWS = 20


async def _fetch_news(code: str, limit: int) -> list:
    import akshare as ak

    df = await run_ds(ak.stock_news_em, symbol=digits(code))
    if df is None or df.empty:
        return []
    today = datetime.now().strftime("%Y-%m-%d")
    rows = []
    for _, r in df.iterrows():
        published = text(r.get("发布时间"))
        if published and published < today:
            continue  # 公司动态仅取当日信息（发布时间 "YYYY-MM-DD HH:MM:SS" 可直接字典序比较）
        rows.append({
            "datetime": published,
            "title": text(r.get("新闻标题")),
            "content": text(r.get("新闻内容")),
            "channels": text(r.get("文章来源")),
            "src": "eastmoney",
            "url": text(r.get("新闻链接")),
        })
    return rows[:max(1, min(int(limit), _MAX_NEWS_ROWS))]


@tool
async def get_stock_news(code: str, limit: int = _MAX_NEWS_ROWS) -> dict:
    """获取个股当日新闻动态（标题、内容、来源、链接）。公司动态只看当日：
    发布时间早于今天的新闻会被过滤，不做历史回看。

    Args:
        code: 股票代码（必填），如 "600519"。
        limit: 最多返回多少条（默认 20）。

    Returns:
        {"ok": True, "data": [{datetime,title,content,channels,src,url}...]} 成功
        （data 可能为空列表，表示当日暂无该股新闻）；失败返回 error。
    """
    code6 = digits(code)
    if not code6:
        return {"ok": False, "error": "公司动态需要提供股票代码"}
    return await call_tool_safe(_fetch_news, code6, limit)
