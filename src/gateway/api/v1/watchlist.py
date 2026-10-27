"""自选股 HTTP 接口：取参 + 校验入参，业务在 services/watchlist.py。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Query, Request

from gateway.domain.stockcode import normalize_code
from gateway.infra.resp import CODE_BAD_REQUEST, ApiError, ok_response
from gateway.infra.web import read_json
from gateway.security.auth import CurrentUser
from gateway.services import watchlist as service

router = APIRouter(prefix="/watchlist", tags=["watchlist"])

_INDEX_MESSAGE = "股票代码格式不正确，应为 SH/SZ/BJ + 6 位数字"


@router.get("")
async def get_watchlist(
    user_id: CurrentUser,
    filter: str = Query(default="all"),
    order: str = Query(default=""),
):
    return ok_response(await service.list_items(user_id, filter or "all", order))


@router.post("")
async def add_watchlist(request: Request, user_id: CurrentUser):
    payload = await read_json(request, 0, "请求参数不合法")

    normalized, ok = normalize_code(str(payload.get("stock_code") or ""))
    if not ok:
        raise ApiError(CODE_BAD_REQUEST, _INDEX_MESSAGE)

    cost, cost_error = service.parse_cost(payload.get("cost_price"))
    if cost_error:
        raise ApiError(CODE_BAD_REQUEST, cost_error)
    quantity, qty_error = service.parse_quantity(payload.get("quantity"))
    if qty_error:
        raise ApiError(CODE_BAD_REQUEST, qty_error)

    item = await service.add_item(
        user_id,
        normalized,
        cost,
        quantity,
        has_cost=payload.get("cost_price") not in (None, ""),
        has_quantity=payload.get("quantity") not in (None, ""),
    )
    return ok_response(item)


async def _refresh_params(request: Request) -> tuple[str, str]:
    """query 优先，缺失时读 JSON body。

    小程序把刷新参数放在 body 里，只读 query 会静默丢掉过滤条件。
    """
    filter_ = request.query_params.get("filter", "")
    order = request.query_params.get("order", "")
    if filter_ == "" or order == "":
        try:
            raw = await request.body()
            if raw:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    if filter_ == "" and isinstance(payload.get("filter"), str):
                        filter_ = payload["filter"]
                    if order == "" and isinstance(payload.get("order"), str):
                        order = payload["order"]
        except Exception:  # noqa: BLE001, S110 - body 为空或不是 JSON，按「没传」处理
            pass
    return (filter_ or "all"), order


@router.post("/refresh")
async def refresh_watchlist(request: Request, user_id: CurrentUser):
    filter_, order = await _refresh_params(request)
    return ok_response(await service.refresh_items(user_id, filter_, order))


@router.delete("/{stock_code}")
async def delete_watchlist(stock_code: str, user_id: CurrentUser):
    normalized, ok = normalize_code(stock_code)
    if not ok:
        raise ApiError(CODE_BAD_REQUEST, _INDEX_MESSAGE)
    return ok_response(await service.remove_item(user_id, normalized))
