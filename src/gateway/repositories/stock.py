"""股票主数据（stock_basic / v_stock_profile）的只读查询。"""

from __future__ import annotations

import logging
from typing import Any

from gateway.adapters.quote.search import SearchResult
from gateway.infra.deps import runtime

logger = logging.getLogger("stocka.gateway.stock")

_LIKE_ESCAPES = {"\\": "\\\\", "%": "\\%", "_": "\\_"}

_PROFILE_SQL = """
SELECT code, market, ts_code, gw_code, name, board, status, is_st,
       COALESCE(industry_l1, '') AS industry_l1,
       COALESCE(industry_l2, '') AS industry_l2,
       COALESCE(industry_l3, '') AS industry_l3,
       COALESCE(array_to_string(concepts, ','), '') AS concepts,
       COALESCE(to_char(list_date, 'YYYY-MM-DD'), '') AS list_date,
       COALESCE(to_char(delist_date, 'YYYY-MM-DD'), '') AS delist_date
FROM v_stock_profile
WHERE code = %s
"""


def _like_escape(text: str) -> str:
    out = []
    for ch in text:
        out.append(_LIKE_ESCAPES.get(ch, ch))
    return "".join(out)


def _digits_of(text: str) -> str:
    """抽出输入里的数字（最多 6 位）：支持 "600519" / "SH600519" / "600519.SH" / "600"。"""
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    return digits[:6]


async def search_local(query: str, limit: int) -> list[SearchResult]:
    """腾讯 smartbox 挂掉或返回空时用主数据表兜底。

    表未建（迁移未执行）只记日志返回空，不把主流程拖失败。
    """
    term = (query or "").strip()
    if not term or limit <= 0:
        return []

    conditions = ["status = 'listed'"]
    args: list[Any] = []
    digits = _digits_of(term)
    if digits:
        conditions.append("code LIKE %s")
        args.append(digits + "%")
    pattern = "%" + _like_escape(term) + "%"
    conditions.append("(name ILIKE %s OR pinyin_abbr ILIKE %s)")
    args.extend([pattern, pattern])

    # 排序：同名优先 → 三元组相似度 → 代码兜底，保证稳定
    sql = (
        "SELECT code, market, name FROM stock_basic WHERE "
        + " AND ".join(conditions)
        + " ORDER BY (name = %s) DESC, similarity(name, %s) DESC, code LIMIT %s"
    )
    args.extend([term, term, limit])

    try:
        async with runtime().pg.pool.connection() as conn:
            cursor = await conn.execute(sql, args)
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 - 主数据不可用时不影响搜索主路径
        logger.info("local stock search failed: q=%s err=%s", query, exc)
        return []
    return [SearchResult(code=row[0], market=row[1], name=row[2] or row[0]) for row in rows]


async def fetch_profile(digits: str) -> dict[str, Any] | None:
    """按 6 位裸码查主数据；None 表示库里没有这只股票。"""
    async with runtime().pg.pool.connection() as conn:
        cursor = await conn.execute(_PROFILE_SQL, (digits,))
        row = await cursor.fetchone()
    if row is None or not row[0]:
        return None
    return {
        "code": row[0],
        "market": row[1],
        "ts_code": row[2],
        "gw_code": row[3],
        "name": row[4],
        "board": row[5],
        "status": row[6],
        "is_st": bool(row[7]),
        "industry_l1": row[8] or "",
        "industry_l2": row[9] or "",
        "industry_l3": row[10] or "",
        "concepts": [part.strip() for part in (row[11] or "").split(",") if part.strip()],
        "list_date": row[12] or "",
        "delist_date": row[13] or "",
    }
