"""Quick Ask 接口：GET /quick-asks 与 POST /quick-asks/click。

返回顺序不能颠倒的两步：**先去重状态落盘，再写响应缓存**。
缓存只有 30 秒，过期后的下一轮必须能看见这一轮已经用过的 code，
否则刷新会立刻撞回同一批股票。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from server.deps import runtime
from server.domain.stockcode import normalize_code
from server.modules.quickask.pool import (
    bump_ask_heat,
    evergreen_pool,
    load_pool,
    now_cst,
)
from server.modules.quickask.sample import (
    clear_seen,
    contains_code,
    count_unseen,
    mark_seen,
    pick_for_session,
    read_last,
    read_seen,
    save_last,
)
from server.modules.quickask.template import build_ask, phase_at
from server.resp import CODE_BAD_REQUEST, CODE_OK, ApiError, json_bytes, ok_response
from server.security.auth import CurrentUser
from server.web import read_json

logger = logging.getLogger("stocka.server.quickask")

router = APIRouter(prefix="/quick-asks", tags=["quickask"])

DEFAULT_COUNT = 4
MIN_COUNT = 3
MAX_COUNT = 6
MAX_SESSION_LEN = 64
RESP_TTL = 30
_CODE_MESSAGE = "股票代码格式不正确，应为 SH/SZ/BJ + 6 位数字"


def clamp_count(raw: object) -> int:
    """越界是 clamp 不是报错：前端传 10 条也照常给 6 条。"""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_COUNT
    if value < MIN_COUNT:
        return MIN_COUNT
    if value > MAX_COUNT:
        return MAX_COUNT
    return value


def sanitize_session(raw: object) -> str:
    """session_id 会拼进 Redis key：只放行字母数字与 - _，其余退回「按用户」维度。"""
    text = raw if isinstance(raw, str) else ""
    if not text or len(text) > MAX_SESSION_LEN:
        return ""
    for ch in text:
        if not (ch.isascii() and (ch.isalnum() or ch in ("-", "_"))):
            return ""
    return text


async def _cache_get(key: str) -> bytes | None:
    try:
        raw = await runtime().redis.get(key)
    except Exception:  # noqa: BLE001 - 缓存是旁路
        return None
    return raw.encode("utf-8") if raw else None


async def _cache_set(key: str, payload: bytes, ttl: int) -> None:
    try:
        await runtime().redis.set(key, payload, ex=ttl)
    except Exception as exc:  # noqa: BLE001
        logger.info("cache quick asks failed: %s", exc)


def _cached_response(payload: bytes, cache_state: str) -> Response:
    return Response(
        content=payload,
        media_type="application/json; charset=utf-8",
        headers={"X-Cache": cache_state},
    )


@router.get("")
async def list_quick_asks(
    user_id: CurrentUser,
    count: str = Query(default=""),
    session_id: str = Query(default=""),
):
    amount = clamp_count(count)
    sid = sanitize_session(session_id)
    cache_key = f"quickask:resp:{user_id}:{sid}:{amount}"

    cached = await _cache_get(cache_key)
    if cached:
        return _cached_response(cached, "HIT")

    pool, source = await load_pool()
    if not pool:
        pool, source = evergreen_pool(), "fallback"

    seen = await read_seen(user_id, sid)
    if count_unseen(pool, seen) < amount:
        # 池子快被这个会话看完了：直接开新一轮，否则后面每次都走
        # 「不够就退回全池」的分支，建议会在这几条里反复打转
        await clear_seen(user_id, sid)
        seen = set()

    picked = pick_for_session(pool, amount, seen, await read_last(user_id, sid))

    # 池子比 count 还小（极端降级）时用常青股补足：宁可多给一条，也不要首屏只剩一两条
    if len(picked) < amount:
        for fallback in evergreen_pool():
            if len(picked) >= amount:
                break
            if not contains_code(picked, fallback.code):
                picked.append(fallback)

    moment = now_cst()
    phase = phase_at(moment)
    used: dict[str, int] = {}
    asks = [build_ask(candidate, phase, moment, used) for candidate in picked]
    codes = [candidate.code for candidate in picked]

    body = json_bytes(
        CODE_OK,
        "ok",
        {
            "quick_asks": [ask.to_dict() for ask in asks],
            "generated_at": moment.isoformat(),
            "pool_size": len(pool),
            "source": source,
        },
    )

    # 顺序不能反：先去重状态，再写响应缓存
    await mark_seen(user_id, sid, codes)
    await save_last(user_id, sid, codes)
    await _cache_set(cache_key, body, RESP_TTL)

    return _cached_response(body, "MISS")


@router.post("/click")
async def click(request: Request, user_id: CurrentUser):
    """只做一件事：把点击记进行为热度。失败不影响用户发问，前端也不等它的结果。"""
    payload = await read_json(request, 0, "参数格式不正确")
    normalized, ok = normalize_code(str(payload.get("stock_code") or ""))
    if not ok:
        raise ApiError(CODE_BAD_REQUEST, _CODE_MESSAGE)

    await bump_ask_heat(normalized)
    return ok_response({"ok": True})
