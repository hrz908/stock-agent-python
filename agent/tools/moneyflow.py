"""资金流工具（数据源：akshare 东财 + 新浪兜底）。

东财口径：主力（超大单+大单）/超大单/大单/中单/小单的净流入净额，单位元。
与原 Tushare 的「买卖双边金额」不同——这里只有净额，故工具出口直接给净额（元），
资金流向卡由 stock 子图发射层换算成万元并做正负拆分（保持前端卡片契约不变）。

可靠性：push2his 历史端点偶发被东财 WAF 断连（换 UA 无效、按 IP 限流），
失败时先兜底直连 push2 主站的当日资金流快照（同源不同节点），
再兜底新浪历史资金流（独立域名：东财整站封 IP 时仍可用）。
新浪只有「超大/大/中/小单」净额，主力净额按「超大单 + 大单」确定性地相加；
其涨跌幅是小数比率，出口乘 100 转成与东财一致的百分数。
"""
from langchain_core.tools import tool

from tools import call_tool_safe
from tools.market import BROWSER_UA, digits, num, run_ds, text, to_market

# 资金流一次最多回多少行（每天一行，东财单次返回近 120 日）
_MAX_MONEYFLOW_ROWS = 30


def _flow_row(d: dict) -> dict:
    """统一净额行结构（金额单位：元）。"""
    return {
        "ts_code": digits(d["code"]),
        "trade_date": d["trade_date"],
        "close": d.get("close"),
        "pct_chg": d.get("pct_chg"),
        "main_net": d.get("main_net"),
        "super_net": d.get("super_net"),
        "large_net": d.get("large_net"),
        "mid_net": d.get("mid_net"),
        "small_net": d.get("small_net"),
    }


async def _fetch_flow_eastmoney_history(code: str) -> list:
    """主路：akshare 近 120 日资金流（日期升序）。"""
    import akshare as ak

    df = await run_ds(
        ak.stock_individual_fund_flow,
        stock=digits(code), market=to_market(code),
    )
    if df is None or df.empty:
        return []
    rows = []
    for _, r in df.iterrows():
        rows.append(_flow_row({
            "code": code,
            "trade_date": text(r.get("日期")).replace("-", ""),
            "close": num(r.get("收盘价")),
            "pct_chg": num(r.get("涨跌幅")),
            "main_net": num(r.get("主力净流入-净额")),
            "super_net": num(r.get("超大单净流入-净额")),
            "large_net": num(r.get("大单净流入-净额")),
            "mid_net": num(r.get("中单净流入-净额")),
            "small_net": num(r.get("小单净流入-净额")),
        }))
    return rows


async def _fetch_flow_snapshot_today(code: str) -> list:
    """兜底：push2 主站当日资金流快照（同源不同节点，WAF 命中率低）。"""
    import requests

    secid = ("1." if to_market(code) == "sh" else "0.") + digits(code)
    url = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
    params = {
        "lmt": "0", "klt": "101", "secid": secid,
        "fields1": "f1,f2,f3,f7",
        # f51日期 f52主力 f53小单 f54中单 f55大单 f56超大单（单位：元）
        "fields2": "f51,f52,f53,f54,f55,f56",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
    }

    def _get() -> dict:
        r = requests.get(url, params=params, headers={
            "User-Agent": BROWSER_UA, "Referer": "https://data.eastmoney.com/",
        }, timeout=8)
        r.raise_for_status()
        return r.json()

    body = await run_ds(_get)
    klines = ((body or {}).get("data") or {}).get("klines") or []
    if not klines:
        return []
    parts = str(klines[-1]).split(",")
    if len(parts) < 6:
        return []
    # 快照行无收盘价/涨跌幅：留空由卡片与模型按缺失处理，不编造
    return [_flow_row({
        "code": code,
        "trade_date": parts[0].replace("-", ""),
        "close": None,
        "pct_chg": None,
        "main_net": num(parts[1]),
        "small_net": num(parts[2]),
        "mid_net": num(parts[3]),
        "large_net": num(parts[4]),
        "super_net": num(parts[5]),
    })]


async def _fetch_flow_sina(code: str, days: int) -> list:
    """兜底 2：新浪历史资金流（与东财不同域名，东财封 IP 时仍可用）。

    字段口径：r0/r1/r2/r3 = 超大/大/中/小单净额；主力净额 = r0 + r1；
    changeratio 是小数比率，乘 100 后与东财的「涨跌幅(%)」一致。
    """
    import requests

    d = digits(code)

    def _get() -> list:
        r = requests.get(
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "MoneyFlow.ssl_qsfx_lscjfb",
            params={
                "page": "1",
                "num": str(max(1, min(int(days), _MAX_MONEYFLOW_ROWS))),
                "sort": "opendate", "asc": "0",
                "daima": f"{to_market(code)}{d}",
            },
            headers={
                "User-Agent": BROWSER_UA,
                "Referer": "https://finance.sina.com.cn/",
            },
            timeout=8,
        )
        r.raise_for_status()
        return r.json()

    body = await run_ds(_get)
    rows = []
    for item in body or []:
        if not isinstance(item, dict):
            continue
        super_net = num(item.get("r0_net"))
        large_net = num(item.get("r1_net"))
        main_net = None
        if super_net is not None or large_net is not None:
            main_net = round((super_net or 0) + (large_net or 0), 4)
        ratio = num(item.get("changeratio"))
        rows.append(_flow_row({
            "code": code,
            "trade_date": str(item.get("opendate") or "").replace("-", ""),
            "close": num(item.get("trade")),
            "pct_chg": None if ratio is None else round(ratio * 100, 2),
            "main_net": main_net,
            "super_net": super_net,
            "large_net": large_net,
            "mid_net": num(item.get("r2_net")),
            "small_net": num(item.get("r3_net")),
        }))
    return rows


async def _fetch_flow(code: str, trade_date: str, days: int) -> list:
    rows = []
    try:
        rows = await _fetch_flow_eastmoney_history(code)
    except Exception:  # noqa: BLE001 - 历史端点被 WAF 断连时继续走兜底
        rows = []
    if not rows:
        try:
            rows = await _fetch_flow_snapshot_today(code)
        except Exception:  # noqa: BLE001 - 东财整站封 IP 时快照也会失败，转新浪
            rows = []
    if not rows:
        rows = await _fetch_flow_sina(code, days)
    if trade_date:
        filtered = [r for r in rows if r["trade_date"] == trade_date]
        rows = filtered or rows  # 兜底源只有近期：请求别的日期时如实返回可得的行
    # 各源返回顺序不一致，统一按交易日期倒序（最新在前）后截断
    rows.sort(key=lambda r: str(r.get("trade_date") or ""), reverse=True)
    return rows[:max(1, min(int(days), _MAX_MONEYFLOW_ROWS))]


@tool
async def get_money_flow(code: str, trade_date: str = "", days: int = 30) -> dict:
    """获取个股资金流向：主力/超大单/大单/中单/小单的净流入净额（单位：元）。
    适合回答「资金在流入还是流出 / 主力动向」类问题。

    Args:
        code: 股票代码。
        trade_date: 交易日期 YYYYMMDD，留空取最近一段。
        days: 留空 trade_date 时回看多少天（默认 30，上限 30）。

    Returns:
        {"ok": True, "data": [{trade_date,close,pct_chg,main_net,super_net,
        large_net,mid_net,small_net}]}（按日期倒序，净额单位元）；
        {"ok": False, "error": "..."} 失败。
    """
    return await call_tool_safe(_fetch_flow, code, trade_date, days)
