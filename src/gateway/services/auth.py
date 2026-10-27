"""登录业务：code → openid → 查/建用户 → 签发 JWT。

只做身份认证：不接收 nickname/avatar。那是微信的用户权限数据，得先拿到身份、
确认库里没有，才值得让用户授权；补齐资料走 PUT /user/profile（那时已有 token）。
"""

from __future__ import annotations

import logging
from typing import Any

from gateway.infra.deps import config, runtime
from gateway.infra.resp import CODE_BAD_GATEWAY, CODE_INTERNAL, ApiError
from gateway.repositories import users as repo
from gateway.security.jwt import sign_token

logger = logging.getLogger("stocka.gateway.auth")

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
    """按 openid 查用户，没有就建（is_new=True）。

    已有用户原样返回、一个字段都不动——重复登录不该改动用户自己设好的资料。
    """
    user = await repo.find_by_openid(openid)
    if user is not None:
        return user, False

    created = await repo.insert_if_absent(openid, DEFAULT_NICKNAME)
    if created is not None:
        return created, True

    # 冲突了：行已被并发请求插入，重新查出来当老用户返回
    user = await repo.find_by_openid(openid)
    if user is None:  # 理论上不可达，防御性兜底
        raise RuntimeError("user vanished after upsert conflict")
    return user, False


async def login(code: str) -> dict[str, Any]:
    try:
        openid = await _openid_of(code)
    except Exception as exc:
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

    return {
        "token": token,
        "user_id": user["id"],
        "nickname": user["nickname"],
        "avatar": user["avatar"],
        "is_new": is_new,
    }
