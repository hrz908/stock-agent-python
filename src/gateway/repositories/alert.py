"""alert_rule 表的读写。全部 SQL 集中在 repositories，服务层不再拼 SQL。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from gateway.infra.deps import runtime

_COLUMNS = (
    "id, user_id, stock_code, stock_name, condition_type, threshold, volume_base, "
    "check_frequency, enabled, created_at, updated_at, last_triggered_at"
)

# UPDATE 的列名来自代码里的固定集合，不接受调用方临时拼字符串
UPDATABLE_COLUMNS = frozenset(
    {
        "condition_type",
        "threshold",
        "volume_base",
        "check_frequency",
        "enabled",
        "last_triggered_at",
    }
)


def row_to_rule(row: Any) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "user_id": int(row[1]),
        "stock_code": row[2],
        "stock_name": row[3] or "",
        "condition_type": row[4],
        "threshold": Decimal(str(row[5])),
        "volume_base": int(row[6] or 0),
        "check_frequency": row[7] or "realtime",
        "enabled": bool(row[8]),
        "created_at": row[9],
        "updated_at": row[10],
        "last_triggered_at": row[11],
    }


async def fetch_rules(
    user_id: int, stock_code: str | None = None, only_enabled: bool = False
) -> list[dict[str, Any]]:
    sql = f"SELECT {_COLUMNS} FROM alert_rule WHERE user_id = %s"
    args: list[Any] = [user_id]
    if stock_code:
        sql += " AND stock_code = %s"
        args.append(stock_code)
    if only_enabled:
        sql += " AND enabled = true"
    sql += " ORDER BY created_at DESC"

    async with runtime().pg.pool.connection() as conn:
        cursor = await conn.execute(sql, args)
        rows = await cursor.fetchall()
    return [row_to_rule(row) for row in rows]


async def fetch_one(user_id: int, rule_id: int) -> dict[str, Any] | None:
    async with runtime().pg.pool.connection() as conn:
        cursor = await conn.execute(
            f"SELECT {_COLUMNS} FROM alert_rule WHERE id = %s AND user_id = %s",
            (rule_id, user_id),
        )
        row = await cursor.fetchone()
    return row_to_rule(row) if row else None


async def exists_enabled(user_id: int, stock_code: str, condition_type: str) -> bool:
    async with runtime().pg.pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM alert_rule WHERE user_id = %s AND stock_code = %s "
            "AND condition_type = %s AND enabled = true",
            (user_id, stock_code, condition_type),
        )
        return await cursor.fetchone() is not None


async def insert(
    user_id: int,
    stock_code: str,
    stock_name: str,
    condition_type: str,
    threshold: Decimal,
    volume_base: int,
    check_frequency: str,
) -> dict[str, Any]:
    async with runtime().pg.pool.connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO alert_rule (user_id, stock_code, stock_name, condition_type, "
            "threshold, volume_base, check_frequency, enabled) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, true) RETURNING {_COLUMNS}",
            (
                user_id,
                stock_code,
                stock_name,
                condition_type,
                threshold,
                volume_base,
                check_frequency,
            ),
        )
        row = await cursor.fetchone()
    return row_to_rule(row)


async def update(user_id: int, rule_id: int, updates: dict[str, Any]) -> None:
    unknown = set(updates) - UPDATABLE_COLUMNS
    if unknown:
        raise ValueError(f"不可更新的列: {sorted(unknown)}")

    sets = ", ".join(f"{column} = %s" for column in updates)
    args = list(updates.values()) + [rule_id, user_id]
    async with runtime().pg.pool.connection() as conn:
        await conn.execute(
            f"UPDATE alert_rule SET {sets}, updated_at = now() "
            "WHERE id = %s AND user_id = %s",
            args,
        )


async def delete(user_id: int, rule_id: int) -> None:
    async with runtime().pg.pool.connection() as conn:
        await conn.execute(
            "DELETE FROM alert_rule WHERE id = %s AND user_id = %s",
            (rule_id, user_id),
        )


async def mark_triggered(user_id: int, rule_id: int) -> None:
    async with runtime().pg.pool.connection() as conn:
        await conn.execute(
            "UPDATE alert_rule SET last_triggered_at = now() WHERE id = %s AND user_id = %s",
            (rule_id, user_id),
        )
