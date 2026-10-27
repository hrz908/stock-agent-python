"""登录 HTTP 接口：POST /api/v1/auth/login。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from gateway.infra.resp import CODE_BAD_REQUEST, ApiError, ok_response
from gateway.infra.web import read_json
from gateway.middleware.guards import login_rate_limit
from gateway.services import auth as service

router = APIRouter(prefix="/auth", tags=["auth"])

LOGIN_BODY_LIMIT = 4 << 10
CODE_MAX_LEN = 128


@router.post("/login", dependencies=[Depends(login_rate_limit)])
async def login(request: Request):
    payload = await read_json(request, LOGIN_BODY_LIMIT, "缺少登录 code")
    code = payload.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ApiError(CODE_BAD_REQUEST, "缺少登录 code")
    if len(code) > CODE_MAX_LEN:
        raise ApiError(CODE_BAD_REQUEST, "登录 code 不合法")

    return ok_response(await service.login(code))
