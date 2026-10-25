"""财务基本面工具（数据源：akshare 东财/新浪）。

- get_valuation:           东财 stock_value_em（个股估值历史，PE/PB/PS/市值）
- get_financial_indicator: 新浪 stock_financial_analysis_indicator（报告期财务指标）
- get_financial_report:    新浪三大报表（利润表 + 现金流量表）+ 指标接口最新一期，
                           并发组装财报速览卡载荷

akshare 是同步 pandas 库：全部经 tools.market.run_ds 丢线程池执行，出口统一过 num()/text() 归一
（DataFrame 的 numpy 标量/NaN 无法直接进 JSON）。
金额口径：报表类为元；估值卡 total_mv/circ_mv 换算成万元（与前端契约一致）。
"""
import asyncio
from datetime import datetime, timedelta

from langchain_core.tools import tool

from tools import call_tool_safe
from tools.market import digits, num, run_ds, text, wan

# 估值工具一次最多回多少行：够回答「最近估值怎么走」，又不会把上下文撑大
_MAX_VALUATION_ROWS = 30
# 财务指标一次最多回多少行（每个报告期一行）
_MAX_FIN_ROWS = 40
# days 参数上限：防止模型要「十年估值」把上下文撑爆
_MAX_RANGE_DAYS = 365


def _date_window(days: int) -> tuple:
    """返回 (start_date, end_date)，按 days 收窄到最近 N 天。"""
    end = datetime.now()
    start = end - timedelta(days=max(1, min(int(days), _MAX_RANGE_DAYS)))
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _sina_stock(code: str) -> str:
    """600519 -> sh600519（新浪报表接口的 stock 参数）。"""
    d = digits(code)
    prefix = "sh" if d.startswith("6") else ("bj" if d.startswith(("4", "8")) else "sz")
    return f"{prefix}{d}"


async def _fetch_valuation(code: str, days: int) -> list:
    """东财个股估值历史，按日期倒序，最多 _MAX_VALUATION_ROWS 行。"""
    import akshare as ak

    df = await run_ds(ak.stock_value_em, symbol=digits(code))
    if df is None or df.empty:
        return []
    df = df.tail(max(1, min(int(days), _MAX_VALUATION_ROWS)))
    d = digits(code)
    suffix = "SH" if d.startswith("6") else ("BJ" if d.startswith(("4", "8")) else "SZ")
    ts = f"{d}.{suffix}"
    # 东财按日期升序，转倒序（最新在前），与原 Tushare daily_basic 口径一致
    out = []
    for _, r in df.iloc[::-1].iterrows():
        out.append({
            "ts_code": ts,
            "trade_date": text(r.get("数据日期")).replace("-", ""),
            "close": num(r.get("当日收盘价")),
            "pe": num(r.get("PE(静)")),
            "pe_ttm": num(r.get("PE(TTM)")),
            "pb": num(r.get("市净率")),
            "ps_ttm": num(r.get("市销率")),
            # 东财估值接口无股息率字段：置空由卡片降级，不编造
            "dv_ttm": None,
            "total_mv": wan(r.get("总市值")),
            "circ_mv": wan(r.get("流通市值")),
        })
    return out


@tool
async def get_valuation(code: str, trade_date: str = "", days: int = 30) -> dict:
    """获取个股每日估值与交易指标：市盈率 PE(静态/TTM)、市净率 PB、市销率 PS、
    总市值、流通市值。适合回答「贵不贵 / 估值多少」类问题。

    Args:
        code: 股票代码，如 "600519" 或 "600519.SH"。
        trade_date: 交易日期 YYYYMMDD，留空取最近一段。
        days: 回看多少天（默认 30，上限 30 行）。

    Returns:
        {"ok": True, "data": [{ts_code,trade_date,close,pe,pe_ttm,pb,ps_ttm,
        dv_ttm,total_mv,circ_mv}...]}（按日期倒序）；{"ok": False, ...} 失败。
    """
    async def _do() -> dict:
        rows = await _fetch_valuation(code, days)
        if trade_date:
            rows = [r for r in rows if r["trade_date"] == trade_date]
        if not rows:
            return {"ok": False, "error": "no valuation data"}
        return {"ok": True, "data": rows}

    return await call_tool_safe(_do)


async def _fetch_fin_indicator(code: str, period: str, limit: int) -> list:
    """新浪报告期财务指标，按报告期倒序，最多 limit 行。"""
    import akshare as ak

    start_year = str(datetime.now().year - 3)
    df = await run_ds(
        ak.stock_financial_analysis_indicator,
        symbol=digits(code), start_year=start_year,
    )
    if df is None or df.empty:
        return []
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "ts_code": digits(code),
            "end_date": text(r.get("日期")),
            "eps": num(r.get("摊薄每股收益(元)")),
            "bps": num(r.get("每股净资产_调整后(元)")),
            "roe": num(r.get("净资产收益率(%)")),
            "grossprofit_margin": num(r.get("销售毛利率(%)")),
            "netprofit_margin": num(r.get("销售净利率(%)")),
            "debt_to_assets": num(r.get("资产负债率(%)")),
            "or_yoy": num(r.get("主营业务收入增长率(%)")),
            "netprofit_yoy": num(r.get("净利润增长率(%)")),
        })
    if period:
        rows = [r for r in rows if r["end_date"] == period]
    # 新浪不保证顺序：显式按报告期倒序后再截断
    rows.sort(key=lambda r: r["end_date"], reverse=True)
    return rows[:max(1, min(int(limit), _MAX_FIN_ROWS))]


@tool
async def get_financial_indicator(code: str, period: str = "", limit: int = _MAX_FIN_ROWS) -> dict:
    """获取个股财务指标：每股收益 EPS、净资产收益率 ROE、销售毛利率、销售净利率、
    资产负债率、每股净资产、营收/净利润同比增速。适合基本面与盈利能力问答。

    Args:
        code: 股票代码。
        period: 报告期 YYYYMMDD（如 "20241231"），留空取最近若干期。
        limit: 最多返回多少个报告期（默认 40）。

    Returns:
        {"ok": True, "data": [{end_date,eps,bps,roe,...}]} 成功（按报告期倒序）；
        {"ok": False, "error": "..."} 失败。
    """
    return await call_tool_safe(
        _fetch_fin_indicator, code, period, limit, timeout=15.0,
    )


# 新浪三大报表的关键列 -> 财报卡字段映射（金额单位：元）
_INCOME_COLS = {
    "end_date": "报告日",
    "revenue": "营业收入",
    "operate_cost": "营业成本",
    "total_profit": "利润总额",
    "n_income": "净利润",
    "n_income_attr_p": "归属于母公司所有者的净利润",
}
_CASHFLOW_COLS = {
    "end_date": "报告日",
    "n_cashflow_act": "经营活动产生的现金流量净额",
}


async def _fetch_sina_report(code: str, sheet: str, cols: dict, period: str) -> dict:
    """拉新浪一张报表，取指定报告期（默认最新一期）并按 cols 映射。"""
    import akshare as ak

    df = await run_ds(
        ak.stock_financial_report_sina,
        stock=_sina_stock(code), symbol=sheet,
    )
    if df is None or df.empty:
        return None
    row = None
    if period:
        hit = df[df["报告日"].astype(str) == period]
        row = hit.iloc[0] if not hit.empty else None
    if row is None:
        row = df.iloc[0]  # 新浪按报告期倒序，首行即最新
    out = {}
    for key, col in cols.items():
        out[key] = text(row.get(col)) if key == "end_date" else num(row.get(col))
    out["end_date"] = out.get("end_date", "")
    return out


@tool
async def get_financial_report(code: str, period: str = "") -> dict:
    """一次性获取个股财报核心数据（组合工具）：财务指标 + 利润表 + 现金流量表。
    适合回答「业绩怎么样 / 财报数据 / 营收和利润」类问题；
    也是小程序「财报速览卡片」的数据源（卡片需要三张表拼齐才完整）。

    Args:
        code: 股票代码，如 "600519" 或 "600519.SH"。
        period: 报告期 YYYYMMDD（如 "20241231"），留空取最新一期。

    Returns:
        {"ok": True, "data": {"indicator": {...}|None, "income": {...}|None,
        "cashflow": {...}|None}}；三张表全部取不到才返回 {"ok": False, "error": ...}。
        单表失败只置 None，不影响其余表。金额单位：元。
    """
    async def _do() -> dict:
        indicator, income, cashflow = await asyncio.gather(
            _fetch_fin_indicator(code, period, 1),
            _fetch_sina_report(code, "利润表", _INCOME_COLS, period),
            _fetch_sina_report(code, "现金流量表", _CASHFLOW_COLS, period),
        )
        data = {
            "indicator": indicator[0] if indicator else None,
            "income": income,
            "cashflow": cashflow,
        }
        if all(v is None for v in data.values()):
            return {"ok": False, "error": "no financial data"}
        return {"ok": True, "data": data}

    # 三路并发（新浪报表页较慢），放宽超时
    return await call_tool_safe(_do, timeout=20.0)
