"""挂在路由上的限流依赖。

与全局 IP 限流分桶，互不共享窗口：
- login：登录是唯一公开的写接口，单独按 IP 收得更紧；
- market：行情/搜索会把一次请求放大成多个上游调用，按登录用户限流，取不到 uid 回落 IP。
"""

from __future__ import annotations

from fastapi import Request

from server.deps import config, runtime
from server.middleware.clientip import client_ip
from server.resp import CODE_TOO_MANY_REQUESTS, ApiError

REJECT_MESSAGE = "请求过于频繁，请稍后再试"


async def login_rate_limit(request: Request) -> None:
    cfg = config()
    ip = client_ip(request, cfg.trusted_proxies)
    allowed = await runtime().limiter.allow(
        "login", ip, cfg.ratelimit.login_per_min, 60.0
    )
    if not allowed:
        raise ApiError(CODE_TOO_MANY_REQUESTS, REJECT_MESSAGE)


async def market_rate_limit(request: Request) -> None:
    cfg = config()
    user_id = getattr(request.state, "user_id", None)
    if isinstance(user_id, int) and user_id > 0:
        ident = f"u:{user_id}"
    else:
        ident = f"ip:{client_ip(request, cfg.trusted_proxies)}"
    allowed = await runtime().limiter.allow(
        "user", ident, cfg.ratelimit.market_per_min, 60.0
    )
    if not allowed:
        raise ApiError(CODE_TOO_MANY_REQUESTS, REJECT_MESSAGE)
