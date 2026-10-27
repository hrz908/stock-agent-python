"""自选股。

三条容易踩空的业务规则：
1. **先拉行情再写库**：行情失败直接 502 不落库，避免「有记录无名称」的脏数据；
2. **移出是软删除**（enabled=false）：硬删会把成本价、建仓数量一起丢掉；
3. **Upsert 只更新「用户这次真的传了」的字段**：重复添加时用户往往不再填成本价，
   无条件覆盖会把他原来的成本冲成 0。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Query, Request

from server import hook
from server.deps import config, runtime
from server.domain.stockcode import display_code, normalize_code
from server.quote.batch import fetch_quotes
from server.quote.cache import batch_get_quotes, batch_save_quotes, save_quote
from server.quote.source import StockSnapshot
from server.resp import (
    CODE_BAD_GATEWAY,
    CODE_BAD_REQUEST,
    CODE_INTERNAL,
    CODE_NOT_FOUND,
    ApiError,
    ok_response,
)
from server.security.auth import CurrentUser
from server.util.amount import money, thousands, with_sign
from server.web import read_json

logger = logging.getLogger("stocka.server.watchlist")

router = APIRouter(prefix="/watchlist", tags=["watchlist"])

NO_QUOTE = "--"
HUNDRED = Decimal("100")
ZERO = Decimal("0")
MAX_COST_PRICE = Decimal("100000000")
MAX_QUANTITY = 100000000

_INDEX_MESSAGE = "股票代码格式不正确，应为 SH/SZ/BJ + 6 位数字"


@dataclass(slots=True)
class WatchRow:
    id: int
    user_id: int
    stock_code: str
    stock_name: str
    cost_price: Decimal
    quantity: int
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class ListEntry:
    item: dict[str, Any]
    pct: Decimal
    profit: Decimal


def _max_items() -> int:
    return config().watchlist_max_items


def _rfc3339(value: datetime) -> str:
    return value.isoformat() if value else ""


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return ZERO


async def _query_rows(user_id: int, *, ordered: bool) -> list[WatchRow]:
    sql = (
        "SELECT id, user_id, stock_code, stock_name, cost_price, quantity, "
        "created_at, updated_at FROM user_watchlist "
        "WHERE user_id = %s AND enabled = true"
    )
    if ordered:
        sql += " ORDER BY created_at DESC"
    async with runtime().pg.pool.connection() as conn:
        cursor = await conn.execute(sql, (user_id,))
        rows = await cursor.fetchall()
    return [
        WatchRow(
            id=int(row[0]),
            user_id=int(row[1]),
            stock_code=row[2],
            stock_name=row[3] or "",
            cost_price=_to_decimal(row[4]),
            quantity=int(row[5] or 0),
            created_at=row[6],
            updated_at=row[7],
        )
        for row in rows
    ]


def _calc_profit(price: Decimal, cost: Decimal, quantity: int) -> tuple[Decimal, Decimal]:
    """浮动盈亏 =（最新价 − 成本价）× 数量；成本为 0 时比例记 0，避免除零。"""
    qty = Decimal(quantity)
    amount = (price - cost) * qty
    cost_value = cost * qty
    rate = amount / cost_value * HUNDRED if cost_value > 0 else ZERO
    return amount, rate


def _build_item(
    row: WatchRow, snapshot: StockSnapshot | None, stale: bool
) -> ListEntry:
    item: dict[str, Any] = {
        "id": row.id,
        "stock_code": row.stock_code,
        "code": display_code(row.stock_code),
        "name": row.stock_name,
        "price": "0",
        "price_text": NO_QUOTE,
        "change_percent": "0",
        "pct_text": NO_QUOTE,
        "up": False,
        "cost_price": f"{row.cost_price.quantize(Decimal('0.0001'))}",
        "quantity": row.quantity,
        "profit_amount": "0.00",
        "profit_rate": "0.00",
        "pending": True,
        "stale": stale,
        "updated_at": _rfc3339(row.updated_at),
    }

    if snapshot is None or snapshot.price == 0:
        # 无行情：价格 0、展示 --，不计入盈亏统计
        return ListEntry(item=item, pct=ZERO, profit=ZERO)

    amount, rate = _calc_profit(snapshot.price, row.cost_price, row.quantity)
    item.update(
        {
            "price": money(snapshot.price),
            "price_text": thousands(snapshot.price),
            "change_percent": f"{snapshot.change_percent.quantize(Decimal('0.01'))}",
            "pct_text": with_sign(snapshot.change_percent) + "%",
            "up": snapshot.change_percent > 0,
            "profit_amount": money(amount),
            "profit_rate": f"{rate.quantize(Decimal('0.01'))}",
            "pending": False,
        }
    )
    return ListEntry(item=item, pct=snapshot.change_percent, profit=amount)


def _assemble(
    rows: list[WatchRow],
    fresh: dict[str, StockSnapshot],
    stale_map: dict[str, StockSnapshot],
    filter_: str,
) -> tuple[list[ListEntry], Decimal, Decimal]:
    skip_pending = filter_ in ("up", "down")
    entries: list[ListEntry] = []
    total_profit = ZERO
    total_cost_value = ZERO

    for row in rows:
        snapshot = fresh.get(row.stock_code)
        is_stale = False
        if snapshot is None:
            snapshot = stale_map.get(row.stock_code)
            is_stale = True

        entry = _build_item(row, snapshot, is_stale)
        if skip_pending and entry.item["pending"]:
            continue
        entries.append(entry)
        if entry.item["pending"]:
            continue  # 无行情不计入盈亏统计
        total_profit += entry.profit
        total_cost_value += row.cost_price * Decimal(row.quantity)

    return entries, total_profit, total_cost_value


def _sum_rate(profit: Decimal, cost_value: Decimal) -> str:
    if cost_value <= 0:
        return "0.00"
    return f"{(profit / cost_value * HUNDRED).quantize(Decimal('0.01'))}"


def _sort_entries(entries: list[ListEntry], filter_: str, order: str) -> None:
    """filter=up 降序、down 升序；all 只有显式传 order 才排序。

    list.sort 是稳定排序：同涨跌幅保持 SQL 顺序，列表不会在两次刷新间随机换位。
    """
    if filter_ == "up":
        entries.sort(key=lambda e: e.pct, reverse=True)
    elif filter_ == "down":
        entries.sort(key=lambda e: e.pct)
    elif filter_ == "all" and order in ("asc", "desc"):
        entries.sort(key=lambda e: e.pct, reverse=(order == "desc"))


def _list_response(
    entries: list[ListEntry],
    total_profit: Decimal,
    total_cost_value: Decimal,
    filter_: str,
    order: str,
) -> dict[str, Any]:
    # 先统计再排序：统计与顺序无关
    up_count = 0
    down_count = 0
    for entry in entries:
        if entry.item["pending"]:
            continue
        if entry.item["up"]:
            up_count += 1
        elif entry.pct < 0:
            down_count += 1

    _sort_entries(entries, filter_, order)
    return {
        "list": [entry.item for entry in entries],
        "total_profit": money(total_profit),
        "total_profit_rate": _sum_rate(total_profit, total_cost_value),
        "up_count": up_count,
        "down_count": down_count,
    }


def _parse_cost(raw: Any) -> tuple[Decimal, str]:
    """未传返回 (0, "")；传了必须是数字、非负且不超上限。"""
    if raw is None or raw == "":
        return ZERO, ""
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return ZERO, "成本价必须是数字"
    if value < 0:
        return ZERO, "成本价不能为负"
    if value > MAX_COST_PRICE:
        return ZERO, "成本价超出允许范围"
    return value, ""


def _parse_quantity(raw: Any) -> tuple[int, str]:
    if raw is None or raw == "":
        return 0, ""
    if isinstance(raw, bool):
        return 0, "持股数量必须是数字"
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0, "持股数量必须是数字"
    if value < 0:
        return 0, "持股数量不能为负"
    if value > MAX_QUANTITY:
        return 0, "持股数量超出允许范围"
    return value, ""


@router.get("")
async def get_watchlist(
    user_id: CurrentUser,
    filter: str = Query(default="all"),
    order: str = Query(default=""),
):
    try:
        rows = await _query_rows(user_id, ordered=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("query watchlist failed: user=%s", user_id)
        raise ApiError(CODE_INTERNAL, "查询自选股失败") from exc

    quotes: dict[str, StockSnapshot] = {}
    if rows:
        # Redis 不可用时不报错，按「无行情」处理（pending: true）
        quotes = await batch_get_quotes([row.stock_code for row in rows])

    entries, total_profit, total_cost_value = _assemble(rows, quotes, {}, filter or "all")
    return ok_response(_list_response(entries, total_profit, total_cost_value, filter or "all", order))


@router.post("")
async def add_watchlist(request: Request, user_id: CurrentUser):
    payload = await read_json(request, 0, "请求参数不合法")

    normalized, ok = normalize_code(str(payload.get("stock_code") or ""))
    if not ok:
        raise ApiError(CODE_BAD_REQUEST, _INDEX_MESSAGE)

    cost, cost_error = _parse_cost(payload.get("cost_price"))
    if cost_error:
        raise ApiError(CODE_BAD_REQUEST, cost_error)
    quantity, qty_error = _parse_quantity(payload.get("quantity"))
    if qty_error:
        raise ApiError(CODE_BAD_REQUEST, qty_error)

    pool = runtime().pg.pool
    try:
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT count(*) FROM user_watchlist WHERE user_id = %s AND enabled = true",
                (user_id,),
            )
            row = await cursor.fetchone()
        count = int(row[0]) if row else 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("count watchlist failed: user=%s", user_id)
        raise ApiError(CODE_INTERNAL, "添加自选股失败") from exc

    if count >= _max_items():
        raise ApiError(CODE_BAD_REQUEST, "自选股数量已达上限")

    # 先拉行情：失败就不落库，避免「有记录无名称」
    snapshot = await runtime().service("market_source").fetch_snapshot(normalized)
    if snapshot is None:
        raise ApiError(CODE_BAD_GATEWAY, "行情获取失败，请稍后重试")
    name = (snapshot.name or "").strip() or normalized

    # 冲突时改哪些列：enabled 必须恢复（移出后重新添加靠它）；
    # 成本价 / 持股数量只「传了才更新」，否则重复添加会把用户原值冲成 0
    has_cost = payload.get("cost_price") not in (None, "")
    has_quantity = payload.get("quantity") not in (None, "")
    update_parts = ["enabled = true"]
    if has_cost:
        update_parts.append("cost_price = EXCLUDED.cost_price")
    if has_quantity:
        update_parts.append("quantity = EXCLUDED.quantity")
    update_parts.append("updated_at = now()")
    args: list[Any] = [user_id, normalized, name, cost, quantity]

    sql = (
        "INSERT INTO user_watchlist (user_id, stock_code, stock_name, cost_price, quantity, enabled, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, true, now()) "
        "ON CONFLICT (user_id, stock_code) DO UPDATE SET "
        + ", ".join(update_parts)
        + " RETURNING id, stock_code, stock_name"
    )
    try:
        async with pool.connection() as conn:
            cursor = await conn.execute(sql, args)
            saved = await cursor.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.exception("upsert watchlist failed: user=%s code=%s", user_id, normalized)
        raise ApiError(CODE_INTERNAL, "添加自选股失败") from exc

    # 缓存失败不影响本次添加
    await save_quote(snapshot)

    return ok_response(
        {
            "id": int(saved[0]) if saved else 0,
            "stock_code": saved[1] if saved else normalized,
            "stock_name": saved[2] if saved else name,
        }
    )


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
                import json

                payload = json.loads(raw)
                if isinstance(payload, dict):
                    if filter_ == "" and isinstance(payload.get("filter"), str):
                        filter_ = payload["filter"]
                    if order == "" and isinstance(payload.get("order"), str):
                        order = payload["order"]
        except Exception:  # noqa: BLE001 - body 为空或不是 JSON，按「没传」处理
            pass
    return (filter_ or "all"), order


@router.post("/refresh")
async def refresh_watchlist(request: Request, user_id: CurrentUser):
    try:
        rows = await _query_rows(user_id, ordered=False)
    except Exception as exc:  # noqa: BLE001
        logger.exception("query watchlist failed: user=%s", user_id)
        raise ApiError(CODE_INTERNAL, "刷新行情失败") from exc

    codes = [row.stock_code for row in rows]
    fresh, failed = await fetch_quotes(codes)
    await batch_save_quotes(list(fresh.values()))

    # 拉取失败的降级读旧缓存；读不到就保持无行情
    stale_map: dict[str, StockSnapshot] = {}
    if failed:
        stale_map = await batch_get_quotes(failed)

    filter_, order = await _refresh_params(request)
    entries, total_profit, total_cost_value = _assemble(rows, fresh, stale_map, filter_)

    # 行情更新完成，异步广播给关心方（提醒检查等），不拖慢本接口响应
    hook.emit_quote_updated_async(user_id)

    return ok_response(
        _list_response(entries, total_profit, total_cost_value, filter_, order)
    )


@router.delete("/{stock_code}")
async def delete_watchlist(stock_code: str, user_id: CurrentUser):
    normalized, ok = normalize_code(stock_code)
    if not ok:
        raise ApiError(CODE_BAD_REQUEST, _INDEX_MESSAGE)

    try:
        async with runtime().pg.pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE user_watchlist SET enabled = false, updated_at = now() "
                "WHERE user_id = %s AND stock_code = %s",
                (user_id, normalized),
            )
            affected = cursor.rowcount or 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("delete watchlist failed: user=%s code=%s", user_id, normalized)
        raise ApiError(CODE_INTERNAL, "移出自选失败") from exc

    if affected == 0:
        # 查不到统一 404，不区分「不存在」与「不是本人的」
        raise ApiError(CODE_NOT_FOUND, "自选不存在或无权限")

    return ok_response({"stock_code": normalized})
