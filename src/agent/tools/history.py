"""历史日线 K 线工具（数据源：baostock query_history_k_data_plus）。

产品约定：K 线走势固定 15 个交易日——窗口收死成 15，模型传更大或更小的
days 都按 15 处理（少于 15 根蜡烛太稀，卡片上看不出形态）。

baostock 直接返回复权后价格（adjustflag：2=前复权 / 1=后复权 / 3=不复权），
不再需要原 Tushare 方案的 adj_factor 手动换算。
baostock 是同步且非线程安全的库：经 tools.market.run_bs 串行执行。
"""
from datetime import datetime, timedelta

from langchain_core.tools import tool

from agent.tools import call_tool_safe
from agent.tools.market import num, run_bs, text, to_baostock_code

# K 线窗口：固定 15 个交易日（产品约定：走势卡至少 15 根，见 kline-card 的 MAX_BARS）
_KLINE_DAYS = 15

# 「N 个交易日」换算成自然日的放大系数 + 节假日余量。
# A 股节假日 + 周末约占自然日的 1/3，2 倍窗口再 +10 天足够兜住春节/国庆长假
_CALENDAR_FACTOR = 2
_CALENDAR_PAD = 10

# 复权方式 -> baostock adjustflag
_ADJ_FLAG = {"qfq": "2", "hfq": "1", "": "3"}

# 日线需要的字段（baostock 列名）
_K_FIELDS = "date,open,high,low,close,preclose,volume,amount,pctChg"


@tool
async def get_history_daily(
    code: str, days: int = _KLINE_DAYS, adj: str = "qfq", end_date: str = ""
) -> dict:
    """获取个股最近 15 个交易日的日 K 线（开高低收、涨跌幅、成交量额），支持前/后复权。
    K 线走势固定 15 个交易日：传更小的 days 也会取满 15，传更大的值截到 15。

    Args:
        code: 股票代码，如 "600519"。
        days: 窗口固定 15 个交易日（产品约定，传入其它值无效）。
        adj: 复权方式，qfq=前复权 / hfq=后复权 / ""=不复权，默认 qfq。
        end_date: 截止日期 YYYYMMDD，留空取到今天。

    Returns:
        {"ok": True, "data": [{trade_date,open,high,low,close,pre_close,pct_chg,
        vol,amount}...]} 成功（按日期升序，固定 15 行，新股/长期停牌不足 15 行；
        vol 单位股、amount 单位元）；{"ok": False, "error": "..."} 失败。
    """
    # 窗口收死：days 不再参与计算，一律取满 15 根（少于 15 根卡片上蜡烛太稀）
    n = _KLINE_DAYS
    end = datetime.now()
    if end_date:
        try:
            end = datetime.strptime(end_date, "%Y%m%d")
        except ValueError:
            pass
    start = end - timedelta(days=n * _CALENDAR_FACTOR + _CALENDAR_PAD)
    flag = _ADJ_FLAG.get(str(adj or "").lower(), "2")

    async def _do() -> dict:
        import baostock as bs

        def _query() -> list:
            rs = bs.query_history_k_data_plus(
                to_baostock_code(code), _K_FIELDS,
                start_date=start.strftime("%Y-%m-%d"),
                end_date=end.strftime("%Y-%m-%d"),
                frequency="d", adjustflag=flag,
            )
            if rs.error_code != "0":
                raise RuntimeError(f"baostock 查询失败: {rs.error_msg}")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            return rows

        raw = await run_bs(_query)
        if not raw:
            return {"ok": False, "error": "no kline data"}
        data = []
        for row in raw:  # baostock 按日期升序返回
            data.append({
                "trade_date": text(row[0]),
                "open": num(row[1]),
                "high": num(row[2]),
                "low": num(row[3]),
                "close": num(row[4]),
                "pre_close": num(row[5]),
                "vol": num(row[6]),
                "amount": num(row[7]),
                "pct_chg": num(row[8]),
            })
        # 窗口兜底截断：节假日多时自然日窗口可能覆盖超过 n 个交易日
        return {"ok": True, "data": data[-n:], "adj": str(adj or "qfq")}

    # baostock 单连接串行往返，放宽超时（默认 8s 对慢时段偏紧）
    return await call_tool_safe(_do, timeout=15.0)
