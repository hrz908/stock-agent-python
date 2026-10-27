"""A股代码表与热门股票池。

两个不同的概念：
- 代码表（load_universe）：全市场 5000+ 只，供个股新闻等按股票采集的源做名称补全；
- 热门池（sync_top_universe）：按当日成交额排序的前 N 只，决定「采集范围」。

选成交额而不是换手率/量比：成交额同时反映价格与成交量，是 A 股「活跃度」最常用的
单一口径；换手率会偏袒小盘股（同样资金量换手率高一个数量级），量比只比当日与过去
五日，跨股票不可比。

取数走新浪排行接口（node=hs_a）：它一次给 100 条且自带 amount，实测在国内云主机上
稳定；东财 push2 的 clist 接口在本机大分页（pz>=100）会被整段拒绝，只能取前 5 条，
当不了主源。
"""
import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ingest.ashare import config
from ingest.ashare.net import HttpClient
from ingest.ashare.util import now_cn

logger = logging.getLogger(__name__)

_CACHE_TTL = 7 * 24 * 3600
SINA_RANK_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
_SINA_PAGE_SIZE = 100
_HISTORY_KEEP_DAYS = 400


# --------------------------------------------------------------------------- 全市场代码表
def _cache_path() -> Path:
    return Path(config.DATA_DIR) / "universe.json"


def load_universe(force: bool = False) -> dict[str, str]:
    """返回 {6 位代码: 简称}。刷新失败时回落到旧缓存，不阻塞其它源。"""
    path = _cache_path()
    cached: dict[str, str] = {}
    fresh = False
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            cached = payload.get("codes") or {}
            fresh = (time.time() - float(payload.get("ts", 0))) < _CACHE_TTL
        except (ValueError, OSError):
            cached = {}

    if cached and fresh and not force:
        return cached

    try:
        import akshare as ak

        df = ak.stock_info_a_code_name()
        code_col = next((c for c in df.columns if str(c).lower() in ("code", "代码", "证券代码")), df.columns[0])
        name_col = next((c for c in df.columns if str(c).lower() in ("name", "名称", "证券简称")), df.columns[1])
        codes = {
            str(row[code_col]).zfill(6): str(row[name_col]).strip()
            for _, row in df.iterrows()
            if str(row[code_col]).strip().isdigit()
        }
        if codes:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"ts": time.time(), "codes": codes}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            logger.info("A股代码表已刷新：%d 只", len(codes))
            return codes
    except Exception as exc:
        logger.warning("A股代码表刷新失败，使用旧缓存（%d 条）：%s", len(cached), exc)

    if cached:
        return cached
    raise RuntimeError("无法获取 A股代码表：akshare 不可用且本地无缓存")


# --------------------------------------------------------------------------- 热门池
@dataclass(slots=True)
class HotStock:
    code: str
    name: str
    amount: float        # 成交额（元）
    turnover: float      # 换手率（%）
    change_pct: float    # 涨跌幅（%）


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def fetch_hot_ranking(top_n: int = 500, client: HttpClient | None = None) -> list[HotStock]:
    """按成交额从高到低取前 top_n 只。停牌（amount 为 0）不占名额。"""
    if top_n <= 0:
        raise ValueError("top_n 必须为正整数")
    client = client or HttpClient(qps=max(0.5, config.EASTMONEY_QPS))
    headers = {"Referer": "https://finance.sina.com.cn", "Accept": "*/*"}

    out: list[HotStock] = []
    seen: set[str] = set()
    page = 1
    max_pages = top_n // _SINA_PAGE_SIZE + 3
    while page <= max_pages and len(out) < top_n:
        payload = client.get_json(SINA_RANK_URL, params={
            "page": page,
            "num": _SINA_PAGE_SIZE,
            "sort": "amount",
            "asc": 0,
            "node": "hs_a",
            "symbol": "",
            "_s_r_a": "page",
        }, headers=headers)
        if not isinstance(payload, list) or not payload:
            if page == 1:
                raise RuntimeError("新浪排行接口返回为空，无法生成热门池")
            break
        for item in payload:
            code = str(item.get("code") or "").strip()
            if len(code) != 6 or not code.isdigit() or code in seen:
                continue
            amount = _to_float(item.get("amount"))
            # 停牌/停市：当天没有成交额，进池也抓不到有效信息
            if amount <= 0:
                continue
            seen.add(code)
            out.append(HotStock(
                code=code,
                name=str(item.get("name") or "").strip(),
                amount=amount,
                turnover=_to_float(item.get("turnoverratio")),
                change_pct=_to_float(item.get("changepercent")),
            ))
            if len(out) >= top_n:
                break
        page += 1

    if not out:
        raise RuntimeError("新浪排行接口未返回任何有成交额的股票")
    # 接口本身按 amount 排序，但翻页过程中可能因实时刷新错位，这里重排一次保证 rank 单调
    out.sort(key=lambda s: s.amount, reverse=True)
    logger.info("热门池取数完成：%d 只，首位 %s（%.1f 亿）", len(out), out[0].name, out[0].amount / 1e8)
    return out[:top_n]


def ensure_universe_schema(store) -> None:
    for table in ("ashare_universe", "ashare_universe_history", "ashare_backfill_progress"):
        try:
            store.conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
            store.conn.commit()
        except Exception as exc:  # psycopg.errors.UndefinedTable；不引入 psycopg 只为这一处
            store.conn.rollback()
            raise RuntimeError(
                f"缺少表 {table}：先执行 "
                "python -m ingest.ashare.cli init-db --file 013_ashare_universe.sql"
            ) from exc


def sync_top_universe(store, top_n: int = 500, *, trade_date: date | None = None) -> dict:
    """拉当日成交额 Top N，整体替换当前池并留一份当日快照。"""
    ensure_universe_schema(store)
    stocks = fetch_hot_ranking(top_n)
    day = trade_date or now_cn().date()

    rows = [
        (s.code, rank, s.name, s.amount, s.turnover, s.change_pct, day)
        for rank, s in enumerate(stocks, 1)
    ]
    with store.conn.cursor() as cur:
        # 整体替换：池子语义是「今天该采谁」，残留昨天的代码会让采集范围只增不减
        cur.execute("DELETE FROM ashare_universe")
        cur.executemany(
            """
            INSERT INTO ashare_universe (code, rank, name, amount, turnover, change_pct, trade_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )
        cur.executemany(
            """
            INSERT INTO ashare_universe_history (trade_date, code, rank, amount)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (trade_date, code) DO UPDATE
               SET rank = EXCLUDED.rank, amount = EXCLUDED.amount
            """,
            [(day, s.code, rank, s.amount) for rank, s in enumerate(stocks, 1)],
        )
        cur.execute(
            "DELETE FROM ashare_universe_history WHERE trade_date < %s::date - %s",
            (day, _HISTORY_KEEP_DAYS),
        )
        dropped = cur.rowcount or 0
    store.conn.commit()

    stats = {
        "trade_date": day.isoformat(),
        "codes": len(stocks),
        "amount_yi": round(sum(s.amount for s in stocks) / 1e8, 2),
        "history_dropped": dropped,
        "head": [{"code": s.code, "name": s.name, "amount_yi": round(s.amount / 1e8, 2)} for s in stocks[:5]],
    }
    logger.info("热门池已更新：%s，%d 只，合计成交额 %.0f 亿",
                day, len(stocks), stats["amount_yi"])
    return stats


def top_codes(store) -> list[str]:
    """当前热门池的代码，按成交额降序。池子为空时返回空列表，由调用方决定是否报错。"""
    try:
        rows = store.conn.execute(
            "SELECT code FROM ashare_universe ORDER BY rank"
        ).fetchall()
    except Exception:
        store.conn.rollback()
        return []
    store.conn.commit()
    return [row["code"] for row in rows]


def top_rows(store, limit: int | None = None) -> list[dict]:
    sql = "SELECT code, rank, name, amount, turnover, change_pct, trade_date FROM ashare_universe ORDER BY rank"
    params: list = []
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    rows = store.conn.execute(sql, params).fetchall()
    store.conn.commit()
    return [dict(r) for r in rows]
