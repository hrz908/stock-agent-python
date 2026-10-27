"""登录态校验依赖。

user_id 只从 Authorization: Bearer 解出，禁止从请求体 / query 读取，防越权。
刻意不支持 ?token=（会落进代理访问日志与浏览器历史）。

agent 侧会用同一 JWT_SECRET 签一个服务 token 反调网关的
/api/v1/stocks/search 与 /api/v1/stock/quote，因此这里只需统一验签即可自然放行。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from server.deps import config
from server.resp import CODE_UNAUTHORIZED, ApiError
from server.security.jwt import parse_token


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if len(header) > 7 and header[:7].lower() == "bearer ":
        return header[7:].strip()
    return ""


async def require_auth(request: Request) -> int:
    token = bearer_token(request)
    try:
        user_id = parse_token(token, config().jwt_secret)
    except ValueError as exc:
        raise ApiError(CODE_UNAUTHORIZED, "未登录或登录已失效") from exc
    if user_id <= 0:
        raise ApiError(CODE_UNAUTHORIZED, "未登录或登录已失效")
    # 供限流依赖与业务 handler 读取（限流在 Auth 之后执行）
    request.state.user_id = user_id
    return user_id


CurrentUser = Annotated[int, Depends(require_auth)]
