"""JWT 签发与校验（HS256）。

与 Go 版 internal/auth/jwt.go 对齐：
- claims 只有 user_id / iat / exp，没有 jti、没有 refresh token；
- 强制校验签名算法必须是 HMAC，拒绝 alg 混淆攻击；
- user_id 兼容 int 与「数字字符串」两种写法（Go 侧为兼容 float64/string 都接受）。
"""

from __future__ import annotations

import time

import jwt as pyjwt

_ALLOWED_ALGS = ["HS256"]


def sign_token(user_id: int, secret: str, ttl_seconds: float) -> str:
    now = int(time.time())
    claims = {
        "user_id": int(user_id),
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    return pyjwt.encode(claims, secret, algorithm="HS256")


def parse_token(token: str, secret: str) -> int:
    """解析并校验 token，返回 user_id；任何失败都抛 ValueError。"""
    if not token:
        raise ValueError("empty token")
    try:
        claims = pyjwt.decode(
            token,
            secret,
            algorithms=_ALLOWED_ALGS,
            options={"require": ["exp"]},
        )
    except pyjwt.PyJWTError as exc:  # 包含过期、签名错误、算法不符
        raise ValueError("invalid token") from exc

    raw = claims.get("user_id")
    if isinstance(raw, bool) or raw is None:
        raise ValueError("missing user_id")
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError("invalid user_id") from exc
    raise ValueError("missing user_id")
