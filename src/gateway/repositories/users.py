"""users 表及其级联数据的读写。

登录、资料、头像、注销四个入口共用同一份列定义，避免各写一遍 SELECT。
"""

from __future__ import annotations

from typing import Any

from gateway.infra.deps import runtime

_COLUMNS = "id, openid, nickname, avatar"


def _row_to_user(row: Any) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "openid": row[1] or "",
        "nickname": row[2] or "",
        "avatar": row[3] or "",
    }


async def find_by_openid(openid: str) -> dict[str, Any] | None:
    async with runtime().pg.pool.connection() as conn:
        row = await (
            await conn.execute(
                f"SELECT {_COLUMNS} FROM users WHERE openid = %s",
                (openid,),
            )
        ).fetchone()
    return _row_to_user(row) if row is not None else None


async def find_by_id(user_id: int) -> dict[str, Any] | None:
    async with runtime().pg.pool.connection() as conn:
        row = await (
            await conn.execute(
                f"SELECT {_COLUMNS} FROM users WHERE id = %s",
                (user_id,),
            )
        ).fetchone()
    return _row_to_user(row) if row is not None else None


async def insert_if_absent(openid: str, nickname: str) -> dict[str, Any] | None:
    """并发首次登录时后到者静默跳过（撞 uk_users_openid），返回 None 表示已存在。

    调用方拿到 None 后应重新查一次，把已有行当老用户返回，而不是回 500。
    """
    async with runtime().pg.pool.connection() as conn:
        row = await (
            await conn.execute(
                f"""
                INSERT INTO users (openid, nickname)
                VALUES (%s, %s)
                ON CONFLICT (openid) DO NOTHING
                RETURNING {_COLUMNS}
                """,
                (openid, nickname),
            )
        ).fetchone()
    return _row_to_user(row) if row is not None else None


async def update_profile(user_id: int, nickname: str, avatar: str) -> None:
    async with runtime().pg.pool.connection() as conn:
        await conn.execute(
            "UPDATE users SET nickname = %s, avatar = %s, updated_at = now() WHERE id = %s",
            (nickname, avatar, user_id),
        )


async def load_avatar(user_id: int) -> str:
    async with runtime().pg.pool.connection() as conn:
        row = await (
            await conn.execute("SELECT avatar FROM users WHERE id = %s", (user_id,))
        ).fetchone()
    return (row[0] if row else "") or ""


async def update_avatar(user_id: int, avatar: str) -> None:
    async with runtime().pg.pool.connection() as conn:
        await conn.execute(
            "UPDATE users SET avatar = %s, updated_at = now() WHERE id = %s",
            (avatar, user_id),
        )


async def purge_account(user_id: int) -> None:
    """硬删当前用户及其业务数据（对话会话与 checkpoint 由 agent 侧持有）。"""
    async with runtime().pg.pool.connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM alert_rule WHERE user_id = %s", (user_id,))
        await conn.execute("DELETE FROM user_watchlist WHERE user_id = %s", (user_id,))
        await conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
