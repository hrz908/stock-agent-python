"""数据类（数值）业务表同步：报告期财务指标 + 每日估值。

这条线不碰 documents / chunks：数值问答要的是精确、可按报告期比较的结构化行，
从 450 字的文本块里抽数字既不准也慢。向量库继续只负责定性内容。

取数口径（决定了同步成本，别随手换源）：
- 财务指标走东财业绩报表 `stock_yjbb_em(date=报告期)`：一次调用拿到全市场某一期的
  EPS / 净资产 / ROE / 毛利率 / 营收 / 净利 / 公告日。5 年只有 20 次调用；
  换成新浪逐股指标接口就是 300 只 × 20 期 = 6000 次，慢两个数量级还更容易被限流。
- 估值走东财 `stock_value_em(symbol)`：逐股，但一次返回整段历史，日更只取最近几行。
  300 只约 5 分钟，放收盘后跑。

写入一律 upsert，且用 COALESCE 保住已有字段：某个源这次没给某列（东财偶尔缺一列），
不能把上一轮已经写好的值擦成 NULL。
"""
import logging
import time
from datetime import date, datetime

from ingest.ashare import config
from ingest.ashare.util import cell, now_cn

logger = logging.getLogger(__name__)

_STATE_DATASET_FINANCE = "finance"
_STATE_DATASET_VALUATION = "valuation"

# 报告期只有四个季末，且表的 report_type 有 CHECK 约束，非季末的日期直接不生成
_QUARTER_ENDS = ("0331", "0630", "0930", "1231")
_REPORT_TYPES = {"0331": "Q1", "0630": "H1", "0930": "Q3", "1231": "FY"}


def ensure_data_schema(store) -> None:
    """校验 009 / 014 的表是否已建；缺表时给可执行提示，而不是让插入报 UndefinedTable。"""
    for table in ("stock_finance_indicator", "stock_valuation_daily", "stock_data_sync_state"):
        try:
            store.conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
            store.conn.commit()
        except Exception as exc:  # psycopg.errors.UndefinedTable，不为此单独 import
            store.conn.rollback()
            raise RuntimeError(
                f"缺少表 {table}：先执行 python -m ingest.ashare.cli init-db "
                "--file 009_stock_finance_indicator.sql --file 014_stock_data_tables.sql"
            ) from exc


def _ak():
    import akshare as ak

    return ak


def _throttle() -> None:
    time.sleep(1.0 / max(0.2, float(config.EASTMONEY_QPS)))


# --------------------------------------------------------------------------- 解析
def _num(row, *names: str) -> float | None:
    """按候选列名取数值；「--」「-」等占位符与空值一律返回 None，不填 0。"""
    text = cell(row, *names)
    if not text:
        return None
    cleaned = text.replace(",", "").replace("%", "").strip()
    for bad in ("--", "-", "—", "None"):
        if cleaned == bad:
            return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _iso_date(value: str) -> str | None:
    """akshare 的日期列可能是 2024-12-31 / 20241231 / 带时分秒的字符串，统一成 ISO。"""
    text = (value or "").strip().split(" ")[0]
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def periods(years: int, until: date | None = None) -> list[str]:
    """最近 N 年出现过的报告期（YYYYMMDD），倒序。未来的报告期不生成。"""
    today = until or now_cn().date()
    out: list[str] = []
    for year in range(today.year - max(1, years), today.year + 1):
        for md in _QUARTER_ENDS:
            raw = f"{year}{md}"
            if datetime.strptime(raw, "%Y%m%d").date() <= today:
                out.append(raw)
    return sorted(set(out), reverse=True)


# --------------------------------------------------------------------------- 财务指标
_FIN_UPSERT = """
INSERT INTO stock_finance_indicator
    (code, report_date, report_type, announce_date, eps_yuan, bps_yuan, roe_pct,
     gross_margin_pct, net_margin_pct, debt_asset_pct, revenue_yoy_pct,
     netprofit_yoy_pct, revenue_yuan, netprofit_yuan, ocf_yuan, source, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (code, report_date) DO UPDATE SET
    report_type       = EXCLUDED.report_type,
    announce_date     = COALESCE(EXCLUDED.announce_date, stock_finance_indicator.announce_date),
    eps_yuan          = COALESCE(EXCLUDED.eps_yuan, stock_finance_indicator.eps_yuan),
    bps_yuan          = COALESCE(EXCLUDED.bps_yuan, stock_finance_indicator.bps_yuan),
    roe_pct           = COALESCE(EXCLUDED.roe_pct, stock_finance_indicator.roe_pct),
    gross_margin_pct  = COALESCE(EXCLUDED.gross_margin_pct, stock_finance_indicator.gross_margin_pct),
    net_margin_pct    = COALESCE(EXCLUDED.net_margin_pct, stock_finance_indicator.net_margin_pct),
    debt_asset_pct    = COALESCE(EXCLUDED.debt_asset_pct, stock_finance_indicator.debt_asset_pct),
    revenue_yoy_pct   = COALESCE(EXCLUDED.revenue_yoy_pct, stock_finance_indicator.revenue_yoy_pct),
    netprofit_yoy_pct = COALESCE(EXCLUDED.netprofit_yoy_pct, stock_finance_indicator.netprofit_yoy_pct),
    revenue_yuan      = COALESCE(EXCLUDED.revenue_yuan, stock_finance_indicator.revenue_yuan),
    netprofit_yuan    = COALESCE(EXCLUDED.netprofit_yuan, stock_finance_indicator.netprofit_yuan),
    ocf_yuan          = COALESCE(EXCLUDED.ocf_yuan, stock_finance_indicator.ocf_yuan),
    source            = EXCLUDED.source,
    updated_at        = now()
"""


def _finance_row(code: str, period: str, row) -> tuple:
    """东财业绩报表一行 -> stock_finance_indicator 一行。

    只映射确定存在的列；资产负债率与经营现金流净额不在该接口里，留 NULL 由
    读取层回落实时接口，不拿「每股经营现金流 × 股本」这种推算值冒充总额。
    """
    # 东财业绩报表的营收列叫「营业总收入-营业总收入」（不是「营业收入」）；
    # 只写营业收入一个名字会静默入库 NULL——行数正常、字段全空，最难发现的那种错
    revenue = _num(row, "营业总收入-营业总收入", "营业收入-营业收入", "营业收入")
    netprofit = _num(row, "净利润-净利润", "净利润")
    net_margin = None
    if revenue and netprofit is not None and revenue != 0:
        net_margin = round(netprofit / revenue * 100, 4)
    return (
        code,
        f"{period[:4]}-{period[4:6]}-{period[6:]}",
        _REPORT_TYPES[period[4:]],
        _iso_date(cell(row, "最新公告日期", "公告日期")),
        _num(row, "每股收益"),
        _num(row, "每股净资产"),
        _num(row, "净资产收益率"),
        _num(row, "销售毛利率"),
        net_margin,
        _num(row, "资产负债率"),
        _num(row, "营业总收入-同比增长", "营业收入-同比增长", "营业收入同比增长"),
        _num(row, "净利润-同比增长", "净利润同比增长"),
        revenue,
        netprofit,
        None,
        "em",
    )


def sync_finance(store, codes: list[str], years: int = 5) -> dict:
    """按报告期同步财务指标。一次调用覆盖全市场一期，再按 codes 过滤入库。"""
    ak = _ak()
    wanted = set(codes)
    stats = {"periods": 0, "rows": 0, "failed_periods": [], "last_period": ""}
    _state_begin(store, _STATE_DATASET_FINANCE)

    for period in periods(years):
        try:
            df = ak.stock_yjbb_em(date=period)
        except Exception as exc:  # 单期失败不能拖垮整轮：其余报告期照常入库
            logger.warning("报告期 %s 取数失败：%s", period, exc)
            stats["failed_periods"].append(period)
            continue
        if df is None or df.empty:
            logger.info("报告期 %s 东财无数据（多为尚未到披露期）", period)
            continue

        rows = []
        for _, row in df.iterrows():
            code = cell(row, "股票代码", "代码").zfill(6)
            if code not in wanted:
                continue
            rows.append(_finance_row(code, period, row))
        if not rows:
            continue

        with store.conn.cursor() as cur:
            cur.executemany(_FIN_UPSERT, rows)
        store.conn.commit()
        stats["periods"] += 1
        stats["rows"] += len(rows)
        # periods() 倒序：只在第一次成功时记，否则会被后面的老报告期覆盖成 2021Q1
        if not stats["last_period"]:
            stats["last_period"] = period
        logger.info("报告期 %s 入库 %d 只", period, len(rows))
        _throttle()

    ok = not stats["failed_periods"] or stats["rows"] > 0
    _state_end(store, _STATE_DATASET_FINANCE, str(stats["last_period"]),
               stats["rows"], ok, ",".join(stats["failed_periods"]) or None)
    return stats


# --------------------------------------------------------------------------- 估值
_VAL_UPSERT = """
INSERT INTO stock_valuation_daily
    (code, trade_date, close_yuan, pe_static, pe_ttm, pb, ps_ttm, dv_ttm_pct,
     total_mv_yuan, circ_mv_yuan, source)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'em')
ON CONFLICT (code, trade_date) DO UPDATE SET
    close_yuan    = COALESCE(EXCLUDED.close_yuan, stock_valuation_daily.close_yuan),
    pe_static     = COALESCE(EXCLUDED.pe_static, stock_valuation_daily.pe_static),
    pe_ttm        = COALESCE(EXCLUDED.pe_ttm, stock_valuation_daily.pe_ttm),
    pb            = COALESCE(EXCLUDED.pb, stock_valuation_daily.pb),
    ps_ttm        = COALESCE(EXCLUDED.ps_ttm, stock_valuation_daily.ps_ttm),
    dv_ttm_pct    = COALESCE(EXCLUDED.dv_ttm_pct, stock_valuation_daily.dv_ttm_pct),
    total_mv_yuan = COALESCE(EXCLUDED.total_mv_yuan, stock_valuation_daily.total_mv_yuan),
    circ_mv_yuan  = COALESCE(EXCLUDED.circ_mv_yuan, stock_valuation_daily.circ_mv_yuan),
    updated_at    = now()
"""


def sync_valuation(store, codes: list[str], days: int = 5) -> dict:
    """逐股同步估值历史，每只只写最近 days 个交易日（接口一次给整段历史）。"""
    ak = _ak()
    stats = {"codes": 0, "rows": 0, "failed": 0, "latest": ""}
    _state_begin(store, _STATE_DATASET_VALUATION)

    for idx, code in enumerate(codes, 1):
        try:
            df = ak.stock_value_em(symbol=code)
        except Exception as exc:
            logger.warning("估值取数失败 %s：%s", code, exc)
            stats["failed"] += 1
            _throttle()
            continue
        if df is None or df.empty:
            stats["failed"] += 1
            _throttle()
            continue

        rows = []
        for _, row in df.tail(max(1, days)).iterrows():
            trade_date = _iso_date(cell(row, "数据日期", "日期"))
            if not trade_date:
                continue
            rows.append((
                code,
                trade_date,
                _num(row, "当日收盘价", "收盘价"),
                _num(row, "PE(静)", "市盈率(静)"),
                _num(row, "PE(TTM)", "市盈率(TTM)"),
                _num(row, "市净率"),
                _num(row, "市销率"),
                _num(row, "股息率", "股息率(TTM)"),
                _num(row, "总市值"),
                _num(row, "流通市值"),
            ))
        if rows:
            with store.conn.cursor() as cur:
                cur.executemany(_VAL_UPSERT, rows)
            store.conn.commit()
            stats["codes"] += 1
            stats["rows"] += len(rows)
            stats["latest"] = max(stats["latest"], rows[-1][1])
        if idx % 50 == 0:
            logger.info("估值进度 %d/%d，已写 %d 行", idx, len(codes), stats["rows"])
        _throttle()

    ok = stats["codes"] > 0
    _state_end(store, _STATE_DATASET_VALUATION, stats["latest"], stats["rows"], ok,
               None if ok else f"{stats['failed']} 只取数失败")
    return stats


# --------------------------------------------------------------------------- 水位
def _state_begin(store, dataset: str) -> None:
    store.conn.execute(
        "INSERT INTO stock_data_sync_state (dataset, last_run_at) VALUES (%s, now()) "
        "ON CONFLICT (dataset) DO UPDATE SET last_run_at = now()",
        (dataset,),
    )
    store.conn.commit()


def _state_end(store, dataset: str, watermark: str, rows: int, ok: bool,
               error: str | None) -> None:
    """失败只记 error、不动 watermark/last_ok_at：下次重跑同一段，重复行由 upsert 挡掉。"""
    store.conn.execute(
        """
        UPDATE stock_data_sync_state
           SET watermark    = CASE WHEN %(ok)s THEN %(wm)s ELSE watermark END,
               last_ok_at   = CASE WHEN %(ok)s THEN now() ELSE last_ok_at END,
               rows_written = rows_written + %(rows)s,
               error        = %(err)s
         WHERE dataset = %(dataset)s
        """,
        {"ok": ok, "wm": watermark, "rows": rows, "err": error, "dataset": dataset},
    )
    store.conn.commit()
