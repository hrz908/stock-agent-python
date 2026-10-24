"""A股主数据同步：股票清单 / 行业 / 概念（迁移 008）。

和文本语料入库是两条线：这里只写 stock_basic / stock_industry / stock_concept，
不碰 documents / chunks，也不做向量化。

三个约定：
- 代码一律 6 位裸码；market / board 由库里的生成列推导，本模块不判交易所；
- 退市只改 status，绝不 DELETE——外键是 CASCADE，删行会连带行业与概念；
- 行业 / 概念按「本轮先 upsert、再删本轮没写到的旧行」整批替换，不做 TRUNCATE。

板块清单主源是东财 push2：它在国内云主机 IP 上会被整段拒绝连接，重试救不了，只能换源。
兜底用新浪（行业走证监会口径 84 个板块、概念 175 个）——它不是最细的分类，
但接口稳定、有成分股；同花顺在 akshare 里只有板块清单与行情，申万的成分页
（`sw_index_third_cons`）连续请求会被判「No tables found」，都当不了兜底。
两套词表不同，std/source 跟着来源写死，且换源后清掉另一源的行——
同一只股票只保留一条现役行业。
"""
import logging
import time
from datetime import UTC, datetime
from typing import NamedTuple

import psycopg
import psycopg.errors

from ashare import config
from ashare.universe import load_universe
from ashare.util import cell, parse_datetime

logger = logging.getLogger(__name__)

# 概念/行业板块接口在 akshare 里走内部 requests，不受 net.HttpClient 限速，这里自己控节奏
_QPS = max(0.2, float(config.EASTMONEY_QPS))

_TABLES = ("stock_basic", "stock_industry", "stock_concept")


def ensure_master_schema(store) -> None:
    """校验 008 的表是否已建；缺表时给出可执行的提示，而不是让插入报 UndefinedTable。"""
    for table in _TABLES:
        try:
            store.conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
            store.conn.commit()
        except psycopg.errors.UndefinedTable as exc:
            store.conn.rollback()
            raise RuntimeError(
                f"缺少表 {table}：先执行 "
                "python -m ashare.cli init-db --file 008_stock_master.sql"
            ) from exc


def _ak():
    import akshare as ak

    return ak


def _throttle() -> None:
    time.sleep(1.0 / _QPS)


def _valid_code(code: str) -> bool:
    return len(code) == 6 and code.isdigit()


def _is_st(name: str) -> bool:
    """名称前缀即 ST 口径（*ST / ST / SST / S*ST），与行情源展示一致。"""
    return name.upper()[:4].startswith(("ST", "*ST", "SST", "S*ST"))


def _listing_dates(ak) -> dict[str, object]:
    """交易所清单里的上市日期。单点失败只跳过该市场，不影响主流程。"""
    calls = (
        (ak.stock_info_sh_name_code, {"symbol": "主板A股"}),
        (ak.stock_info_sh_name_code, {"symbol": "科创板"}),
        (ak.stock_info_sz_name_code, {"symbol": "A股列表"}),
        (ak.stock_info_bj_name_code, {}),
    )
    out: dict[str, object] = {}
    for fn, kwargs in calls:
        try:
            df = fn(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 上市日期是增强项，缺了不阻塞
            logger.warning("上市日期取数失败(%s %s)：%s", getattr(fn, "__name__", fn), kwargs, exc)
            continue
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            code = cell(row, "证券代码", "A股代码", "代码")
            if not _valid_code(code):
                continue
            dt = parse_datetime(cell(row, "上市日期", "上市时间"))
            if dt:
                out[code] = dt.date()
    return out


class Board(NamedTuple):
    """板块：name 入库用，ref 查成分用（东财用板块名，新浪用 label），code 存 board_code。"""

    name: str
    ref: str
    code: str


_KIND_CN = {"industry": "行业", "concept": "概念"}
_PROVIDER_CN = {"em": "东财", "sina": "新浪"}


def _board_list_em(ak, kind: str) -> list[Board]:
    fn = ak.stock_board_industry_name_em if kind == "industry" else ak.stock_board_concept_name_em
    df = fn()
    if df is None or df.empty:
        return []
    out: list[Board] = []
    for _, row in df.iterrows():
        name = cell(row, "板块名称", "名称", "概念名称")
        if name:
            out.append(Board(name, name, cell(row, "板块代码", "代码")))
    return out


# 新浪的板块口径：`行业` 是证监会分类（84 个板块），`概念` 175 个。
# 行业覆盖约 3100 只（全市场 5577），比东财窄，但接口稳定——先保住 profile 与同业工具。
_SINA_INDICATOR = {"industry": "行业", "concept": "概念"}


def _board_list_sina(ak, kind: str) -> list[Board]:
    """新浪板块：label 既是成分接口参数，也当 board_code（新浪不给单独板块代码）。"""
    df = ak.stock_sector_spot(indicator=_SINA_INDICATOR[kind])
    if df is None or df.empty:
        return []
    out: list[Board] = []
    for _, row in df.iterrows():
        name = cell(row, "板块")
        label = cell(row, "label")
        if name and label:
            out.append(Board(name, label, label))
    return out


def _board_list(ak, kind: str) -> tuple[list[Board], str]:
    """板块清单 + 取数源：东财优先，push2 被拒/为空时退新浪。"""
    try:
        boards = _board_list_em(ak, kind)
        if boards:
            return boards, "em"
        logger.warning("东财%s板块清单为空，改用新浪", _KIND_CN[kind])
    except Exception as exc:  # noqa: BLE001 - 主路失败才走兜底
        logger.warning("东财%s板块清单失败（%s），改用新浪", _KIND_CN[kind], exc)

    try:
        boards = _board_list_sina(ak, kind)
    except Exception as exc:
        raise RuntimeError(
            f"东财与新浪的{_KIND_CN[kind]}板块清单都取不到：{exc}"
        ) from exc
    if not boards:
        raise RuntimeError(f"新浪{_KIND_CN[kind]}板块清单为空，终止同步")
    logger.info("改用新浪%s板块清单：%d 个", _KIND_CN[kind], len(boards))
    return boards, "sina"


def _board_members(ak, kind: str, board: Board, provider: str) -> list[str]:
    """板块成分 -> 6 位代码列表。"""
    if provider == "sina":
        df = ak.stock_sector_detail(sector=board.ref)
    else:
        fn = (ak.stock_board_industry_cons_em if kind == "industry"
              else ak.stock_board_concept_cons_em)
        df = fn(symbol=board.ref)
    if df is None or df.empty:
        return []
    codes = []
    for _, row in df.iterrows():
        # 列名各家不同：东财中文列名、新浪小写 code；统一取后 6 位数字
        code = cell(row, "代码", "证券代码", "股票代码", "code")
        digits = "".join(ch for ch in code if ch.isdigit())[-6:]
        if _valid_code(digits):
            codes.append(digits)
    return codes


def _known_codes(store) -> set[str]:
    rows = store.conn.execute("SELECT code FROM stock_basic").fetchall()
    store.conn.commit()
    return {r["code"] for r in rows}


def sync_basic(store) -> dict:
    """股票清单：code/name/上市日/ST 标记，并把消失的代码标成退市。"""
    codes = load_universe(force=True)
    if not codes:
        raise RuntimeError("A股代码表为空，终止同步")
    dates = _listing_dates(_ak())
    rows = [(code, name, dates.get(code), _is_st(name)) for code, name in codes.items()]

    with store.conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO stock_basic (code, name, list_date, is_st, source, updated_at)
            VALUES (%s, %s, %s, %s, 'akshare', now())
            ON CONFLICT (code) DO UPDATE
               SET name       = EXCLUDED.name,
                   list_date  = COALESCE(EXCLUDED.list_date, stock_basic.list_date),
                   is_st      = EXCLUDED.is_st,
                   status     = 'listed',
                   delist_date = NULL,
                   source     = EXCLUDED.source,
                   updated_at = now()
            """,
            rows,
        )
        delisted = 0
        # 上游偶发只返回部分市场，条数异常少时不做退市判定，否则会把半个市场标死
        if len(codes) < config.MASTER_MIN_CODES:
            logger.warning("代码表仅 %d 条（阈值 %d），跳过退市判定",
                           len(codes), config.MASTER_MIN_CODES)
        else:
            cur.execute(
                """
                UPDATE stock_basic
                   SET status = 'delisted',
                       delist_date = COALESCE(delist_date, now()::date),
                       updated_at = now()
                 WHERE status = 'listed' AND NOT (code = ANY(%s::text[]))
                """,
                (list(codes),),
            )
            delisted = cur.rowcount or 0
    store.conn.commit()
    logger.info("股票清单同步完成：%d 只，新增退市 %d 只，含上市日 %d 只",
                len(codes), delisted, len(dates))
    return {"codes": len(codes), "with_list_date": len(dates), "delisted": delisted}


def sync_industry(store, limit: int = 0) -> dict:
    """行业（一级）：一只股票一行，l2/l3 留空；std 记数据源。"""
    ak = _ak()
    run_start = datetime.now(UTC)
    boards, provider = _board_list(ak, "industry")
    if limit > 0:
        boards = boards[:limit]

    known = _known_codes(store)
    rows: list[tuple[str, str, str]] = []
    failed = 0
    for index, board in enumerate(boards, 1):
        _throttle()
        try:
            members = _board_members(ak, "industry", board, provider)
        except Exception as exc:  # noqa: BLE001 - 单个板块失败不拖垮整轮
            failed += 1
            logger.warning("行业板块取数失败(%s)：%s", board.name, exc)
            continue
        rows.extend((code, provider, board.name) for code in members if code in known)
        if index % 20 == 0:
            logger.info("行业进度 %d/%d", index, len(boards))

    deleted = _replace_industry(store, rows, run_start, failed, provider)
    listed, covered = _industry_coverage(store)
    logger.info("行业同步完成：源 %s，板块 %d，成分 %d，失败 %d，清理旧行 %d，覆盖率 %d/%d",
                _PROVIDER_CN.get(provider, provider), len(boards), len(rows), failed, deleted,
                covered, listed)
    if listed and covered / listed < 0.9:
        # 兜底源覆盖更窄是已知代价，但要让人一眼看到，别以为同步失败
        logger.warning("行业覆盖率仅 %.0f%%：当前是兜底源 %s，东财恢复后重跑可回到全覆盖",
                       covered * 100.0 / listed, _PROVIDER_CN.get(provider, provider))
    return {"provider": provider, "boards": len(boards), "rows": len(rows), "failed": failed,
            "deleted": deleted, "listed": listed, "covered": covered}


def _industry_coverage(store) -> tuple[int, int]:
    """(上市股数, 有行业的上市股数)：换源后 coverage 会掉，运维要能查。"""
    row = store.conn.execute(
        """
        SELECT (SELECT count(*) FROM stock_basic WHERE status = 'listed') AS listed,
               (SELECT count(DISTINCT i.code) FROM stock_industry i
                 JOIN stock_basic b ON b.code = i.code
                WHERE b.status = 'listed') AS covered
        """
    ).fetchone()
    store.conn.commit()
    return int(row["listed"]), int(row["covered"])


def _replace_industry(store, rows: list[tuple[str, str, str]], run_start,
                      failed: int, provider: str) -> int:
    with store.conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO stock_industry (code, std, l1, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (code, std) DO UPDATE
               SET l1 = EXCLUDED.l1, l2 = NULL, l3 = NULL, updated_at = now()
            """,
            rows,
        )
        # 有板块失败时不清旧行：那一批成分本轮没写进来，删了等于凭空丢数据
        if failed:
            logger.warning("本轮有 %d 个板块失败，跳过旧行清理", failed)
            store.conn.commit()
            return 0
        # std <> 当前源：换源后旧词表的行必须清掉，否则一只股票挂两条互不相容的行业
        cur.execute(
            "DELETE FROM stock_industry WHERE updated_at < %s OR std <> %s",
            (run_start, provider),
        )
        deleted = cur.rowcount or 0
    store.conn.commit()
    return deleted


def sync_concept(store, limit: int = 0) -> dict:
    """概念板块：多对多，一只股票可挂多个概念；source 记数据源。"""
    ak = _ak()
    run_start = datetime.now(UTC)
    boards, provider = _board_list(ak, "concept")
    if limit > 0:
        boards = boards[:limit]

    known = _known_codes(store)
    rows: list[tuple[str, str, str, str]] = []
    failed = 0
    for index, board in enumerate(boards, 1):
        _throttle()
        try:
            members = _board_members(ak, "concept", board, provider)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("概念板块取数失败(%s)：%s", board.name, exc)
            continue
        rows.extend((board.name, code, board.code, provider)
                    for code in members if code in known)
        if index % 50 == 0:
            logger.info("概念进度 %d/%d", index, len(boards))

    deleted = _replace_concept(store, rows, run_start, failed, provider)
    logger.info("概念同步完成：源 %s，板块 %d，成分 %d，失败 %d，清理旧行 %d",
                _PROVIDER_CN.get(provider, provider), len(boards), len(rows), failed, deleted)
    return {"provider": provider, "boards": len(boards), "rows": len(rows),
            "failed": failed, "deleted": deleted}


def _replace_concept(store, rows: list[tuple[str, str, str, str]], run_start,
                     failed: int, provider: str) -> int:
    with store.conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO stock_concept (concept, code, board_code, source, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (concept, code) DO UPDATE
               SET board_code = EXCLUDED.board_code,
                   source     = EXCLUDED.source,
                   updated_at = now()
            """,
            rows,
        )
        if failed:
            logger.warning("本轮有 %d 个板块失败，跳过旧行清理", failed)
            store.conn.commit()
            return 0
        # 与行业同理：换源后另一套概念词表的行要清掉，同名的由主键冲突合并
        cur.execute(
            "DELETE FROM stock_concept WHERE updated_at < %s OR source <> %s",
            (run_start, provider),
        )
        deleted = cur.rowcount or 0
    store.conn.commit()
    return deleted


def sync_master(store, only: list[str] | None = None,
                concept_limit: int = 0, industry_limit: int = 0) -> dict:
    """按需同步三类主数据；顺序固定（行业/概念依赖 stock_basic 的外键）。"""
    ensure_master_schema(store)
    wanted = [x for x in (only or ["basic", "industry", "concept"])]
    stats: dict = {}
    if "basic" in wanted:
        stats["basic"] = sync_basic(store)
    if "industry" in wanted:
        stats["industry"] = sync_industry(store, limit=industry_limit)
    if "concept" in wanted:
        stats["concept"] = sync_concept(store, limit=concept_limit)
    return stats
