"""提醒规则 HTTP 接口：只负责取参、校验入参、拼响应，业务在 services/alert.py。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from gateway.domain.stockcode import normalize_code
from gateway.infra.resp import CODE_BAD_REQUEST, ApiError, ok_response
from gateway.infra.web import read_json
from gateway.security.auth import CurrentUser
from gateway.services import alert as service

router = APIRouter(prefix="/alerts", tags=["alerts"])

_CODE_MESSAGE = "股票代码格式不正确，应为 SH/SZ/BJ + 6 位数字"


def _parse_rule_id(raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        raise ApiError(CODE_BAD_REQUEST, "规则 id 不合法")
    return value


@router.post("")
async def create_alert(request: Request, user_id: CurrentUser):
    payload = await read_json(request, 0, "请求参数不合法")

    normalized, ok = normalize_code(str(payload.get("stock_code") or ""))
    if not ok:
        raise ApiError(CODE_BAD_REQUEST, _CODE_MESSAGE)

    cond_type = service.normalize_condition(payload.get("condition_type"))
    if cond_type not in service.VALID_CONDITIONS:
        raise ApiError(CODE_BAD_REQUEST, "条件类型不合法")

    threshold, error = service.parse_threshold(cond_type, payload.get("threshold"))
    if error:
        raise ApiError(CODE_BAD_REQUEST, error)

    volume_base, error = service.normalize_volume_base(cond_type, payload.get("volume_base"))
    if error:
        raise ApiError(CODE_BAD_REQUEST, error)

    frequency = (payload.get("check_frequency") or "").strip() or service.FREQ_REALTIME
    if not service.valid_frequency(frequency):
        raise ApiError(CODE_BAD_REQUEST, "检查频率不合法")

    stock_name = (payload.get("stock_name") or "").strip()
    if len(stock_name) > service.STOCK_NAME_MAX:
        raise ApiError(CODE_BAD_REQUEST, "股票名称过长")

    item = await service.create_rule(
        user_id,
        stock_code=normalized,
        condition_type=cond_type,
        threshold=threshold,
        volume_base=volume_base,
        check_frequency=frequency,
        stock_name=stock_name,
    )
    return ok_response(item)


@router.get("")
async def list_alerts(user_id: CurrentUser, stock_code: str = Query(default="")):
    raw = (stock_code or "").strip()
    code = None
    if raw:
        code, ok = normalize_code(raw)
        if not ok:
            raise ApiError(CODE_BAD_REQUEST, _CODE_MESSAGE)

    return ok_response(await service.list_rules(user_id, code))


@router.put("/{rule_id}")
async def update_alert(rule_id: str, request: Request, user_id: CurrentUser):
    identifier = _parse_rule_id(rule_id)
    payload = await read_json(request, 0, "请求参数不合法")
    return ok_response(await service.update_rule(user_id, identifier, payload))


@router.delete("/{rule_id}")
async def delete_alert(rule_id: str, user_id: CurrentUser):
    identifier = _parse_rule_id(rule_id)
    return ok_response(await service.delete_rule(user_id, identifier))
