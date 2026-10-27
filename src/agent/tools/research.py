"""个股研报工具（数据源：akshare 东财 stock_research_report_em）。

研报卡（research_card）的数据源。东财「个股研报」天然按代码返回，
一行一篇研报，含机构 / 东财评级 / 报告名称 / 发布日期 / PDF 链接。

刻意不为卡片编数据：
- 东财不提供「目标价」，卡片的 targetPrice / upside 留空由卡片降级显示「--」；
- 「盈利预测调整方向」需要同一机构的历史预测做比对，数据源没有，forecastDir 留空。
宁可留空，也不让模型或工具层凭空生成数值（与项目「不编造数据」约定一致）。

代码格式：东财研报接口要 6 位裸代码（tools.market.digits）。
"""
from langchain_core.tools import tool

from agent.tools import call_tool_safe, take_latest, to_ts_code
from agent.tools.market import digits, num, run_ds, text

# 研报一次最多回多少篇（卡片按行发射，一行一篇）
_MAX_RESEARCH_ROWS = 20

# 报告类型：数据源没有独立类型字段，从报告名称归纳。
# 顺序敏感：越具体的关键词排越前（「深度」优先于「点评」）
_REPORT_TYPE_RULES = (
    ("深度", "深度报告"),
    ("首次覆盖", "首次覆盖"),
    ("点评", "点评报告"),
    ("跟踪", "跟踪报告"),
    ("年报", "财报点评"),
    ("中报", "财报点评"),
    ("季报", "财报点评"),
)

# 盈利预测列的前缀：接口按年份给列（"2026-盈利预测-收益"），年份逐年变化，
# 这里按列名匹配而不是写死年份，避免跨年失效
_EPS_COL_KEY = "盈利预测-收益"
_PE_COL_KEY = "盈利预测-市盈率"


def _report_type(title: str) -> str:
    """报告名称 -> 报告类型标签；无法归纳时回落到「个股研报」。"""
    for kw, label in _REPORT_TYPE_RULES:
        if kw in title:
            return label
    return "个股研报"


def _forecast_col(columns, key: str):
    """取最近一年的盈利预测列名（列名形如 "2026-盈利预测-收益"）。"""
    hits = sorted(str(c) for c in columns if key in str(c))
    return hits[0] if hits else None


async def _fetch_research(code: str, limit: int) -> list:
    import akshare as ak

    df = await run_ds(ak.stock_research_report_em, symbol=digits(code))
    if df is None or df.empty:
        return []

    eps_col = _forecast_col(df.columns, _EPS_COL_KEY)
    pe_col = _forecast_col(df.columns, _PE_COL_KEY)

    rows = []
    for _, r in df.iterrows():
        title = text(r.get("报告名称"))
        org = text(r.get("机构"))
        if not title and not org:
            continue
        rows.append({
            "ts_code": to_ts_code(text(r.get("股票代码")) or code),
            "name": text(r.get("股票简称")),
            "org": org,
            "rating": text(r.get("东财评级")),
            "report_type": _report_type(title),
            # 日期形如 "2026-09-21"（部分行带时间，统一切到日）
            "publish_date": text(r.get("日期"))[:10],
            "summary": title,
            "source_name": org or "东方财富",
            "source_url": text(r.get("报告PDF链接")),
            "industry": text(r.get("行业")),
            "eps_forecast": num(r.get(eps_col)) if eps_col else None,
            "pe_forecast": num(r.get(pe_col)) if pe_col else None,
        })
    # 数据源不保证顺序：显式按发布日期倒序再截断，拿到的永远是「最近 N 篇」
    return take_latest(rows, key="publish_date",
                       limit=max(1, min(int(limit), _MAX_RESEARCH_ROWS)))


@tool
async def get_research_reports(code: str, limit: int = 3) -> dict:
    """获取个股的机构研报观点（机构、评级、报告名称、发布日期、原文 PDF 链接）。

    回答「机构怎么看」「券商给什么评级」「最近有什么研报」这类问题用这个工具。
    注意：机构评级只是券商观点，不是投资建议。

    Args:
        code: 股票代码（必填），如 "600519"。
        limit: 最多返回多少篇（默认 3，按发布日期倒序）。

    Returns:
        {"ok": True, "data": {"ts_code":..., "name":..., "rows":[...]}} 成功
        （rows 可能为空列表，表示暂无该股研报）；失败返回 error。
    """
    code6 = digits(code)
    if not code6:
        return {"ok": False, "error": "机构研报需要提供股票代码"}
    res = await call_tool_safe(_fetch_research, code6, limit)
    if not res.get("ok"):
        return res  # 取数失败：原样回传降级信封
    rows = res.get("data") or []
    return {
        "ok": True,
        "data": {
            "ts_code": to_ts_code(code6),
            "name": rows[0].get("name") if rows else "",
            "rows": rows,
        },
    }
