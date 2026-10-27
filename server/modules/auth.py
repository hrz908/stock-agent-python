"""登录模块：POST /api/v1/auth/login。

只做身份认证：code → openid → 查/建用户 → 签发 JWT，并回传库里已有资料。
不接收 nickname/avatar：那是微信的用户权限数据，得先拿到身份、确认库里没有
才值得让用户授权；补齐资料走 PUT /user/profile（那时已有 token）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

from server.deps import config, runtime
from server.middleware.guards import login_rate_limit
from server.resp import CODE_BAD_GATEWAY, CODE_BAD_REQUEST, CODE_INTERNAL, ApiError, ok_response
from server.security.jwt import sign_token
from server.web import read_json

logger = logging.getLogger("stocka.server.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

LOGIN_BODY_LIMIT = 4 << 10
CODE_MAX_LEN = 128
# 该默认昵称同时是前端判断「资料未完善」的依据（与小程序 DEFAULT_NICKNAME 一致）
DEFAULT_NICKNAME = "微信用户"
DEMO_OPENID = "demo"


async def _openid_of(code: str) -> str:
    """配了 AppID/Secret 就走微信；没配则退化为固定演示账号，保证本地链路可跑通。"""
    client = runtime().services.get("wechat")
    if client is not None and client.enabled:
        return await client.code2session(code.strip())
    # prod 由启动自检强制要求微信凭据，不会走到这里
    return DEMO_OPENID


async def _find_or_create_user(openid: str) -> tuple[dict[str, Any], bool]:
    """按 openid 查用户，没有就建（isNew=True）。

    已有用户原样返回、一个字段都不动——重复登录不该改动用户自己设好的资料。
    并发首次登录：两个请求都会走到 INSERT，用 ON CONFLICT DO NOTHING 让后到者
    静默跳过（撞 uk_users_openid），再查一次拿回已有行，否则它会拿到 500。
    """
    pool = runtime().pg.pool
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT id, openid, nickname, avatar FROM users WHERE openid = %s",
                (openid,),
            )
        ).fetchone()
        if row is not None:
            return _user_dict(row), False

        inserted = await (
            await conn.execute(
                """
                INSERT INTO users (openid, nickname)
                VALUES (%s, %s)
                ON CONFLICT (openid) DO NOTHING
                RETURNING id, openid, nickname, avatar
                """,
                (openid, DEFAULT_NICKNAME),
            )
        ).fetchone()
        if inserted is not None:
            return _user_dict(inserted), True

        # 冲突了：行已被并发请求插入，重新查出来当老用户返回
        row = await (
            await conn.execute(
                "SELECT id, openid, nickname, avatar FROM users WHERE openid = %s",
                (openid,),
            )
        ).fetchone()
        if row is None:  # 理论上不可达，防御性兜底
            raise RuntimeError("user vanished after upsert conflict")
        return _user_dict(row), False


def _user_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "openid": row[1] or "",
        "nickname": row[2] or "",
        "avatar": row[3] or "",
    }


@router.post("/login", dependencies=[Depends(login_rate_limit)])
async def login(request: Request):
    payload = await read_json(request, LOGIN_BODY_LIMIT, "缺少登录 code")
    code = payload.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ApiError(CODE_BAD_REQUEST, "缺少登录 code")
    if len(code) > CODE_MAX_LEN:
        raise ApiError(CODE_BAD_REQUEST, "登录 code 不合法")

    try:
        openid = await _openid_of(code)
    except Exception as exc:  # noqa: BLE001 - 上游任何失败都转成 502
        logger.warning("login resolve openid failed: %s", exc)
        raise ApiError(CODE_BAD_GATEWAY, "微信登录失败，请重试") from exc

    try:
        user, is_new = await _find_or_create_user(openid)
    except Exception as exc:
        logger.exception("login upsert user failed: openid=%s", openid)
        raise ApiError(CODE_INTERNAL, "登录失败") from exc

    cfg = config()
    try:
        token = sign_token(user["id"], cfg.jwt_secret, cfg.jwt_ttl)
    except Exception as exc:
        logger.exception("sign token failed: user=%s", user["id"])
        raise ApiError(CODE_INTERNAL, "登录失败") from exc

    return ok_response(
        {
            "token": token,
            "user_id": user["id"],
            "nickname": user["nickname"],
            "avatar": user["avatar"],
            "is_new": is_new,
        }
    )
