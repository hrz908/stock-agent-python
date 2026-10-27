"""user_watchlist 表的读写。软删除语义：enabled=false 即「已移出」。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from gateway.infra.deps import runtime

ZERO = Decimal(0)


@dataclass(slots=True)
class WatchRow:
    id: int
    user_id: int
    stock_code: str
    stock_name: str
    cost_price: Decimal
    quantity: int
    created_at: datetime
    updated_at: datetime


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return ZERO


async def query_rows(user_id: int, *, ordered: bool) -> list[WatchRow]:
    sql = (
        "SELECT id, user_id, stock_code, stock_name, cost_price, quantity, "
        "created_at, updated_at FROM user_watchlist "
        "WHERE user_id = %s AND enabled = true"
    )
    if ordered:
        sql += " ORDER BY created_at DESC"
    async with runtime().pg.pool.connection() as conn:
        cursor = await conn.execute(sql, (user_id,))
        rows = await cursor.fetchall()
    return [
        WatchRow(
            id=int(row[0]),
            user_id=int(row[1]),
            stock_code=row[2],
            stock_name=row[3] or "",
            cost_price=_to_decimal(row[4]),
            quantity=int(row[5] or 0),
            created_at=row[6],
            updated_at=row[7],
        )
        for row in rows
    ]


async def count_enabled(user_id: int) -> int:
    async with runtime().pg.pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT count(*) FROM user_watchlist WHERE user_id = %s AND enabled = true",
            (user_id,),
        )
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def upsert(
    user_id: int,
    stock_code: str,
    stock_name: str,
    cost_price: Decimal,
    quantity: int,
    *,
    update_cost: bool,
    update_quantity: bool,
) -> tuple[int, str, str]:
    """冲突时改哪些列由调用方决定：

    enabled 必须恢复（移出后重新添加靠它）；成本价 / 持股数量只「传了才更新」，
    否则重复添加会把用户原值冲成 0。
    """
    update_parts = ["enabled = true"]
    if update_cost:
        update_parts.append("cost_price = EXCLUDED.cost_price")
    if update_quantity:
        update_parts.append("quantity = EXCLUDED.quantity")
    update_parts.append("updated_at = now()")

    sql = (
        "INSERT INTO user_watchlist "
        "(user_id, stock_code, stock_name, cost_price, quantity, enabled, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, true, now()) "
        "ON CONFLICT (user_id, stock_code) DO UPDATE SET "
        + ", ".join(update_parts)
        + " RETURNING id, stock_code, stock_name"
    )
    args = (user_id, stock_code, stock_name, cost_price, quantity)
    async with runtime().pg.pool.connection() as conn:
        cursor = await conn.execute(sql, args)
        saved = await cursor.fetchone()
    if saved is None:
        return 0, stock_code, stock_name
    return int(saved[0]), saved[1], saved[2]


async def soft_delete(user_id: int, stock_code: str) -> int:
    """移出只置 enabled=false，不物理删行；返回受影响行数。"""
    async with runtime().pg.pool.connection() as conn:
        cursor = await conn.execute(
            "UPDATE user_watchlist SET enabled = false, updated_at = now() "
            "WHERE user_id = %s AND stock_code = %s",
            (user_id, stock_code),
        )
        return cursor.rowcount or 0
