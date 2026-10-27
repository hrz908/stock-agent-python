"""价格提醒规则与触发检查。

几条必须原样保留的业务语义（每一条都是踩过坑的）：
- 价格/涨跌幅为 0（停牌、自建源缺字段）时 **不判**：否则 0 <= 正阈值会让「跌破」类恒真；
- `pct_down` 前端填的是正数「跌多少」，比较时要取负；
- `condition_type=volume` 的快照里没有成交量字段，规则可存但**永不自动触发**（已知未完成，别顺手“修好”）；
- 判定条件变了就清空 `last_triggered_at`，让它能重新提醒；只改 `enabled` 不清；
- 停用/删除都要清防抖锁，否则残留锁会挡住后续推送；
- 同用户+同股票+同条件只允许一条启用规则，应用层先查、DB 唯一索引兜底。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Query, Request

from server import hook
from server.deps import runtime
from server.domain.stockcode import normalize_code
from server.quote.cache import batch_get_quotes
from server.quote.source import StockSnapshot
from server.resp import (
    CODE_BAD_REQUEST,
    CODE_CONFLICT,
    CODE_INTERNAL,
    CODE_NOT_FOUND,
    ApiError,
    ok_response,
)
from server.security.auth import CurrentUser
from server.util.amount import money
from server.util.unique import unique
from server.web import read_json

logger = logging.getLogger("stocka.server.alert")

router = APIRouter(prefix="/alerts", tags=["alerts"])

COND_PRICE_UP = "price_up"
COND_PRICE_DOWN = "price_down"
COND_PCT_UP = "pct_up"
COND_PCT_DOWN = "pct_down"
COND_VOLUME = "volume"

FREQ_REALTIME = "realtime"
FREQ_CLOSE = "close"

VALID_CONDITIONS = frozenset(
    {COND_PRICE_UP, COND_PRICE_DOWN, COND_PCT_UP, COND_PCT_DOWN, COND_VOLUME}
)
VALID_FREQUENCIES = frozenset({FREQ_REALTIME, FREQ_CLOSE})
VALID_VOLUME_BASES = frozenset({5, 10, 20})

ALERT_LOCK_TTL = 60
STOCK_NAME_MAX = 100
# A 股 15:00 收盘；收盘确认型只在收盘后判。取服务器本地时间（部署时区应为 Asia/Shanghai）
CLOSE_HOUR = 15

_CODE_MESSAGE = "股票代码格式不正确，应为 SH/SZ/BJ + 6 位数字"


def _normalize_condition(value: Any) -> str:
    """统一小写去空格，否则 " price_up" 会被判成非法。"""
    return value.strip().lower() if isinstance(value, str) else ""


def _valid_frequency(value: str) -> bool:
    return value in VALID_FREQUENCIES


def _parse_threshold(cond_type: str, raw: Any) -> tuple[Decimal, str]:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0), "阈值必须是数字"

    if cond_type in (COND_PRICE_UP, COND_PRICE_DOWN) and value <= 0:
        return Decimal(0), "价格阈值必须大于 0"
    elif cond_type in (COND_PCT_UP, COND_PCT_DOWN) and (value <= 0 or value > 100):
        return Decimal(0), "涨跌幅阈值需在 0 ~ 100 之间"
    elif cond_type == COND_VOLUME and (value < Decimal("0.1") or value > 100):
        return Decimal(0), "成交量倍数需在 0.1 ~ 100 之间"
    return value, ""


def _normalize_volume_base(cond_type: str, raw: Any) -> tuple[int, str]:
    """只有成交量异动需要基准周期，且必须是 5/10/20；其他条件一律归 0。"""
    if cond_type != COND_VOLUME:
        return 0, ""
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        return 0, "基准周期只能是 5 / 10 / 20 日均量"
    if value == 0:
        # 传 0 表示「用默认 20 日均量」，不是报错
        return 20, ""
    if value not in VALID_VOLUME_BASES:
        return 0, "基准周期只能是 5 / 10 / 20 日均量"
    return value, ""


def _fmt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _to_item(rule: dict[str, Any], snapshot: StockSnapshot | None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": rule["id"],
        "stock_code": rule["stock_code"],
        "stock_name": rule["stock_name"],
        "condition_type": rule["condition_type"],
        "threshold": money(rule["threshold"]),
        "volume_base": rule["volume_base"],
        "check_frequency": rule["check_frequency"],
        "enabled": rule["enabled"],
        "current_price": None,
        "change_percent": None,
        "last_triggered_at": _fmt(rule.get("last_triggered_at")),
        "created_at": _fmt(rule.get("created_at")),
    }
    if snapshot is not None:
        item["current_price"] = money(snapshot.price)
        item["change_percent"] = f"{snapshot.change_percent.quantize(Decimal('0.01'))}"
    return item


_COLUMNS = (
    "id, user_id, stock_code, stock_name, condition_type, threshold, volume_base, "
    "check_frequency, enabled, created_at, updated_at, last_triggered_at"
)


def _row_to_rule(row: Any) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "user_id": int(row[1]),
        "stock_code": row[2],
        "stock_name": row[3] or "",
        "condition_type": row[4],
        "threshold": Decimal(str(row[5])),
        "volume_base": int(row[6] or 0),
        "check_frequency": row[7] or FREQ_REALTIME,
        "enabled": bool(row[8]),
        "created_at": row[9],
        "updated_at": row[10],
        "last_triggered_at": row[11],
    }


def _lock_key(user_id: int, rule: dict[str, Any]) -> str:
    """带条件类型：同一股票的多条不同条件规则互不干扰。"""
    return f"alert:lock:{user_id}:{rule['stock_code']}:{rule['condition_type']}"


async def _del_lock(user_id: int, rule: dict[str, Any]) -> None:
    try:
        await runtime().redis.delete(_lock_key(user_id, rule))
    except Exception as exc:  # noqa: BLE001 - 清理失败只记日志
        logger.info("del alert lock failed: code=%s err=%s", rule["stock_code"], exc)


async def _fetch_rules(
    user_id: int, stock_code: str | None = None, only_enabled: bool = False
) -> list[dict[str, Any]]:
    sql = f"SELECT {_COLUMNS} FROM alert_rule WHERE user_id = %s"
    args: list[Any] = [user_id]
    if stock_code:
        sql += " AND stock_code = %s"
        args.append(stock_code)
    if only_enabled:
        sql += " AND enabled = true"
    sql += " ORDER BY created_at DESC"

    async with runtime().pg.pool.connection() as conn:
        cursor = await conn.execute(sql, args)
        rows = await cursor.fetchall()
    return [_row_to_rule(row) for row in rows]


async def _fetch_one(user_id: int, rule_id: int) -> dict[str, Any] | None:
    async with runtime().pg.pool.connection() as conn:
        cursor = await conn.execute(
            f"SELECT {_COLUMNS} FROM alert_rule WHERE id = %s AND user_id = %s",
            (rule_id, user_id),
        )
        row = await cursor.fetchone()
    return _row_to_rule(row) if row else None


def _parse_rule_id(raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        raise ApiError(CODE_BAD_REQUEST, "规则 id 不合法")
    return value


# ---------------------------------------------------------------- 接口


@router.post("")
async def create_alert(request: Request, user_id: CurrentUser):
    payload = await read_json(request, 0, "请求参数不合法")

    normalized, ok = normalize_code(str(payload.get("stock_code") or ""))
    if not ok:
        raise ApiError(CODE_BAD_REQUEST, _CODE_MESSAGE)

    cond_type = _normalize_condition(payload.get("condition_type"))
    if cond_type not in VALID_CONDITIONS:
        raise ApiError(CODE_BAD_REQUEST, "条件类型不合法")

    threshold, error = _parse_threshold(cond_type, payload.get("threshold"))
    if error:
        raise ApiError(CODE_BAD_REQUEST, error)

    volume_base, error = _normalize_volume_base(cond_type, payload.get("volume_base"))
    if error:
        raise ApiError(CODE_BAD_REQUEST, error)

    frequency = (payload.get("check_frequency") or "").strip() or FREQ_REALTIME
    if not _valid_frequency(frequency):
        raise ApiError(CODE_BAD_REQUEST, "检查频率不合法")

    stock_name = (payload.get("stock_name") or "").strip()
    if len(stock_name) > STOCK_NAME_MAX:
        raise ApiError(CODE_BAD_REQUEST, "股票名称过长")

    pool = runtime().pg.pool
    try:
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT id FROM alert_rule WHERE user_id = %s AND stock_code = %s "
                "AND condition_type = %s AND enabled = true",
                (user_id, normalized, cond_type),
            )
            if await cursor.fetchone() is not None:
                raise ApiError(CODE_CONFLICT, "该股票的该条件提醒已存在")

            cursor = await conn.execute(
                "INSERT INTO alert_rule (user_id, stock_code, stock_name, condition_type, "
                "threshold, volume_base, check_frequency, enabled) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, true) RETURNING {_COLUMNS}",
                (
                    user_id,
                    normalized,
                    stock_name,
                    cond_type,
                    threshold,
                    volume_base,
                    frequency,
                ),
            )
            row = await cursor.fetchone()
    except ApiError:
        raise
    except Exception as exc:
        # 唯一索引冲突：并发下两个请求同时通过了「先查」，这里兜底成 409
        if "uk_alert_user_stock_cond" in str(exc) or "duplicate key" in str(exc):
            raise ApiError(CODE_CONFLICT, "该股票的该条件提醒已存在") from exc
        logger.exception("create alert rule failed: user=%s code=%s", user_id, normalized)
        raise ApiError(CODE_INTERNAL, "创建提醒失败") from exc

    return ok_response(_to_item(_row_to_rule(row), None))


@router.get("")
async def list_alerts(user_id: CurrentUser, stock_code: str = Query(default="")):
    raw = (stock_code or "").strip()
    code = None
    if raw:
        code, ok = normalize_code(raw)
        if not ok:
            raise ApiError(CODE_BAD_REQUEST, _CODE_MESSAGE)

    try:
        rules = await _fetch_rules(user_id, code)
    except Exception as exc:
        logger.exception("list alert rules failed: user=%s", user_id)
        raise ApiError(CODE_INTERNAL, "查询提醒失败") from exc

    # 批量读当前价，前端可直接展示「距离目标还差多少」
    quotes = await batch_get_quotes(unique([rule["stock_code"] for rule in rules]))
    return ok_response([_to_item(rule, quotes.get(rule["stock_code"])) for rule in rules])


@router.put("/{rule_id}")
async def update_alert(rule_id: str, request: Request, user_id: CurrentUser):
    identifier = _parse_rule_id(rule_id)
    payload = await read_json(request, 0, "请求参数不合法")

    # 用 id + user_id 定位，查不到统一 404；刻意不区分「不存在」与「不是本人的」
    rule = await _fetch_one(user_id, identifier)
    if rule is None:
        raise ApiError(CODE_NOT_FOUND, "规则不存在或无权限")

    # 条件类型最终值：请求带了用新的，否则沿用库里的；阈值校验要跟着最终值走
    cond_type = rule["condition_type"]
    new_condition = payload.get("condition_type")
    if new_condition is not None:
        cond_type = _normalize_condition(new_condition)
        if cond_type not in VALID_CONDITIONS:
            raise ApiError(CODE_BAD_REQUEST, "条件类型不合法")

    updates: dict[str, Any] = {}
    if new_condition is not None:
        updates["condition_type"] = cond_type

    if payload.get("threshold") is not None:
        threshold, error = _parse_threshold(cond_type, payload.get("threshold"))
        if error:
            raise ApiError(CODE_BAD_REQUEST, error)
        updates["threshold"] = threshold

    if payload.get("volume_base") is not None:
        volume_base, error = _normalize_volume_base(cond_type, payload.get("volume_base"))
        if error:
            raise ApiError(CODE_BAD_REQUEST, error)
        updates["volume_base"] = volume_base
    elif cond_type != COND_VOLUME:
        # 从成交量切成其它条件时基准周期必须归 0，否则库里残留 20/10/5
        updates["volume_base"] = 0

    if payload.get("check_frequency") is not None:
        frequency = str(payload.get("check_frequency")).strip()
        if not _valid_frequency(frequency):
            raise ApiError(CODE_BAD_REQUEST, "检查频率不合法")
        updates["check_frequency"] = frequency

    if updates:
        # 判定条件变了就清掉上次触发时间，让它能重新提醒
        updates["last_triggered_at"] = None

    if payload.get("enabled") is not None:
        updates["enabled"] = bool(payload.get("enabled"))

    if not updates:
        snapshot = await _cached_snapshot(rule["stock_code"])
        return ok_response(_to_item(rule, snapshot))

    sets = ", ".join(f"{column} = %s" for column in updates)
    args = list(updates.values()) + [identifier, user_id]
    try:
        async with runtime().pg.pool.connection() as conn:
            await conn.execute(
                f"UPDATE alert_rule SET {sets}, updated_at = now() "
                "WHERE id = %s AND user_id = %s",
                args,
            )
    except Exception as exc:
        logger.exception("update alert rule failed: id=%s", identifier)
        raise ApiError(CODE_INTERNAL, "修改提醒失败") from exc

    # 停用即解锁：清掉防抖锁，避免残留锁挡住后续推送
    if payload.get("enabled") is False:
        await _del_lock(user_id, rule)

    rule.update(updates)
    snapshot = await _cached_snapshot(rule["stock_code"])
    return ok_response(_to_item(rule, snapshot))


@router.delete("/{rule_id}")
async def delete_alert(rule_id: str, user_id: CurrentUser):
    identifier = _parse_rule_id(rule_id)

    rule = await _fetch_one(user_id, identifier)
    if rule is None:
        raise ApiError(CODE_NOT_FOUND, "规则不存在或无权限")

    try:
        async with runtime().pg.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM alert_rule WHERE id = %s AND user_id = %s",
                (identifier, user_id),
            )
    except Exception as exc:
        logger.exception("delete alert rule failed: id=%s", identifier)
        raise ApiError(CODE_INTERNAL, "删除提醒失败") from exc

    # 删除后清防抖锁，否则残留锁会挡住同一股票后续新建的规则
    await _del_lock(user_id, rule)
    return ok_response({"id": identifier})


async def _cached_snapshot(code: str) -> StockSnapshot | None:
    quotes = await batch_get_quotes([code])
    return quotes.get(code)


# ---------------------------------------------------------------- 触发检查


def _is_hit(rule: dict[str, Any], snapshot: StockSnapshot | None) -> bool:
    # 停牌 / 自建接口返 0 时价格为 0，0 <= 正阈值会让「跌破」类恒真，用户会收到假提醒
    if snapshot is None or snapshot.price == 0:
        return False

    cond = rule["condition_type"]
    threshold = rule["threshold"]
    if cond == COND_PRICE_UP:
        return snapshot.price >= threshold
    if cond == COND_PRICE_DOWN:
        return snapshot.price <= threshold
    if cond == COND_PCT_UP:
        return snapshot.change_percent >= threshold
    if cond == COND_PCT_DOWN:
        # 前端填的是「跌多少」的正数，比较时取负
        return snapshot.change_percent <= -threshold
    if cond == COND_VOLUME:
        # 快照里没有成交量字段：规则可存，但暂不自动判触发（已知未完成）
        return False
    return False


def _build_wecom_text(snapshot: StockSnapshot, rule: dict[str, Any], now: datetime) -> str:
    name = rule["stock_name"] or snapshot.name
    lines = [
        "📊 股票提醒",
        f"{name}（{rule['stock_code']}）{_condition_label(rule['condition_type'])}",
        f"当前价：{money(snapshot.price)}",
        f"涨跌幅：{snapshot.change_percent.quantize(Decimal('0.01'))}%",
        f"触发阈值：{money(rule['threshold'])}",
        f"时间：{now:%Y-%m-%d %H:%M:%S}",
    ]
    return "\n".join(lines)


def _condition_label(cond_type: str) -> str:
    return {
        COND_PRICE_UP: "价格突破",
        COND_PRICE_DOWN: "价格跌破",
        COND_PCT_UP: "涨幅提醒",
        COND_PCT_DOWN: "跌幅提醒",
        COND_VOLUME: "成交量异动",
    }.get(cond_type, "提醒触发")


async def _safe_push(channel: str, stock_code: str, coro) -> None:
    """旁路推送：不在请求链路里，必须自带异常兜底，否则一次失败会打断检查循环。"""
    try:
        await coro
    except Exception as exc:  # noqa: BLE001
        logger.info("wecom %s push failed: code=%s err=%s", channel, stock_code, exc)


async def check_alerts(user_id: int) -> None:
    """检查该用户的提醒规则，命中就推企业微信（应用消息 + 群机器人两条通道）。

    防重复：SETNX alert:lock:{uid}:{code}:{cond}，TTL 60 秒；写不进去说明 60 秒内已推过。
    """
    try:
        rules = await _fetch_rules(user_id, only_enabled=True)
    except Exception as exc:  # noqa: BLE001
        logger.info("query alert rules failed: user=%s err=%s", user_id, exc)
        return
    if not rules:
        return

    quotes = await batch_get_quotes(unique([rule["stock_code"] for rule in rules]))
    now = datetime.now()
    wecom = runtime().services.get("wecom")

    for rule in rules:
        snapshot = quotes.get(rule["stock_code"])
        if snapshot is None:
            continue  # 没行情不判

        # 收盘确认型只在收盘后判定，盘中即时型每次刷新都判
        if rule["check_frequency"] == FREQ_CLOSE and now.hour < CLOSE_HOUR:
            continue
        if not _is_hit(rule, snapshot):
            continue

        try:
            acquired = await runtime().redis.set(
                _lock_key(user_id, rule), str(int(now.timestamp())), nx=True, ex=ALERT_LOCK_TTL
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("set alert lock failed: code=%s err=%s", rule["stock_code"], exc)
            continue
        if not acquired:
            continue  # 60 秒内已推过

        if wecom is not None and (wecom.enabled or wecom.webhook_enabled):
            content = _build_wecom_text(snapshot, rule, now)
            if wecom.enabled:
                asyncio.create_task(
                    _safe_push("app", rule["stock_code"], wecom.send_text_to_all(content))
                )
            if wecom.webhook_enabled:
                asyncio.create_task(
                    _safe_push("webhook", rule["stock_code"], wecom.send_text_to_webhook(content))
                )

        try:
            async with runtime().pg.pool.connection() as conn:
                await conn.execute(
                    "UPDATE alert_rule SET last_triggered_at = now() WHERE id = %s AND user_id = %s",
                    (rule["id"], user_id),
                )
        except Exception as exc:  # noqa: BLE001
            logger.info("update last_triggered_at failed: id=%s err=%s", rule["id"], exc)


# 订阅「行情已更新」：自幂等注册，避免同一条提醒被检查两遍
hook.register("alert", check_alerts)
