"""热度加权抽样 + 板块多样性 + 会话去重。

三层去重（会话已见 → 相邻刷新 → 板块多样性）中任何一层都不能把候选池抽干：
去重是为了新鲜感，不是为了把池子抽干，宁可放宽也要凑够条数。
"""

from __future__ import annotations

import json
import logging
import random

from gateway.infra.deps import runtime
from gateway.services.quickask.models import Candidate

logger = logging.getLogger("stocka.gateway.quickask.sample")

SEEN_TTL = 30 * 60
LAST_TTL = 30 * 60
# 单次结果同板块上限：热度榜天然被题材股占据，不设约束时四条建议可能全是同一个板块
MAX_PER_SECTOR = 2

# 相邻刷新至少换 2 支：先排除上一轮，剩余够数才采用
MIN_FRESH = 2


def weight_of(candidate: Candidate) -> float:
    """给热度分加一个底：0 分的股票如果权重是 0 就永远抽不到，池子会长期只剩头部那几只。"""
    return candidate.score + 1 if candidate.score > 0 else 1.0


def sample_diverse(
    pool: list[Candidate], k: int, sector_limit: int = MAX_PER_SECTOR
) -> list[Candidate]:
    """热度加权抽样 + 板块多样性软约束；抽样不放回。"""
    if k <= 0 or not pool:
        return []

    picked: list[Candidate] = []
    rest = list(pool)
    sector_count: dict[str, int] = {}

    while len(picked) < k and rest:
        batch = weighted_sample(rest, k - len(picked))
        if not batch:
            break
        for candidate in batch:
            rest = remove_code(rest, candidate.code)

        for candidate in batch:
            if len(picked) >= k:
                break
            # 板块扎堆的直接丢掉，下一轮会从剩余池里再抽
            if candidate.sector and sector_count.get(candidate.sector, 0) >= sector_limit:
                continue
            if candidate.sector:
                sector_count[candidate.sector] = sector_count.get(candidate.sector, 0) + 1
            picked.append(candidate)

    # 多样性是软约束：热门股全挤在少数板块时宁可重复板块，也不能少给一条
    if len(picked) < k:
        for candidate in weighted_sample(pool, len(pool)):
            if len(picked) >= k:
                break
            if contains_code(picked, candidate.code):
                continue
            picked.append(candidate)

    return picked


def weighted_sample(pool: list[Candidate], k: int) -> list[Candidate]:
    """轮盘赌：按热度权重抽 k 支，抽中即移出池子；全池权重为 0 时退化为均匀抽样。"""
    if k <= 0 or not pool:
        return []

    rest = list(pool)
    picked: list[Candidate] = []
    total = sum(weight_of(candidate) for candidate in rest)

    while len(picked) < k and rest and total > 0:
        target = random.random() * total
        accumulated = 0.0
        index = len(rest) - 1
        for position, candidate in enumerate(rest):
            accumulated += weight_of(candidate)
            if accumulated >= target:
                index = position
                break
        chosen = rest[index]
        picked.append(chosen)
        total -= weight_of(chosen)
        rest.pop(index)

    while len(picked) < k and rest:
        index = random.randrange(len(rest))
        picked.append(rest.pop(index))

    return picked


def remove_code(pool: list[Candidate], code: str) -> list[Candidate]:
    for index, candidate in enumerate(pool):
        if candidate.code == code:
            return pool[:index] + pool[index + 1 :]
    return pool


def contains_code(pool: list[Candidate], code: str) -> bool:
    return any(candidate.code == code for candidate in pool)


# ---------------------------------------------------------------- 会话去重状态


def _seen_key(uid: int, sid: str) -> str:
    return f"quickask:seen:{uid}:{sid}"


def _last_key(uid: int, sid: str) -> str:
    return f"quickask:last:{uid}:{sid}"


def _redis_ready() -> bool:
    try:
        return runtime().redis_ok
    except RuntimeError:
        return False


async def read_seen(uid: int, sid: str) -> set[str]:
    if not _redis_ready():
        return set()
    try:
        members = await runtime().redis.smembers(_seen_key(uid, sid))
    except Exception as exc:  # noqa: BLE001
        logger.info("read quick ask seen failed: %s", exc)
        return set()
    return {str(member) for member in members}


async def mark_seen(uid: int, sid: str, codes: list[str]) -> None:
    if not _redis_ready() or not codes:
        return
    key = _seen_key(uid, sid)
    try:
        pipe = runtime().redis.pipeline(transaction=False)
        for code in codes:
            pipe.sadd(key, code)
        pipe.expire(key, SEEN_TTL)
        await pipe.execute()
    except Exception as exc:  # noqa: BLE001
        logger.info("mark quick ask seen failed: %s", exc)


async def read_last(uid: int, sid: str) -> set[str]:
    if not _redis_ready():
        return set()
    try:
        raw = await runtime().redis.get(_last_key(uid, sid))
    except Exception:  # noqa: BLE001
        return set()
    if not raw:
        return set()
    try:
        codes = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return set()
    return {str(code) for code in codes} if isinstance(codes, list) else set()


async def save_last(uid: int, sid: str, codes: list[str]) -> None:
    if not _redis_ready() or not codes:
        return
    try:
        await runtime().redis.set(
            _last_key(uid, sid),
            json.dumps(codes, ensure_ascii=False, separators=(",", ":")),
            ex=LAST_TTL,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("save quick ask last failed: %s", exc)


async def clear_seen(uid: int, sid: str) -> None:
    if not _redis_ready():
        return
    try:
        await runtime().redis.delete(_seen_key(uid, sid))
    except Exception as exc:  # noqa: BLE001
        logger.info("clear quick ask seen failed: %s", exc)


def count_unseen(pool: list[Candidate], seen: set[str]) -> int:
    if not seen:
        return len(pool)
    return sum(1 for candidate in pool if candidate.code not in seen)


def exclude_codes(pool: list[Candidate], skip: set[str]) -> list[Candidate]:
    if not skip:
        return pool
    return [candidate for candidate in pool if candidate.code not in skip]


def pick_for_session(
    pool: list[Candidate], count: int, seen: set[str], last: set[str]
) -> list[Candidate]:
    """三层去重：会话已见 → 相邻刷新 → 板块多样性。"""
    candidates = exclude_codes(pool, seen)
    if len(candidates) < count:
        # 池子快被看完了：去重是为了新鲜感，不是为了把池子抽干
        candidates = pool

    if len(candidates) > count:
        fresh = exclude_codes(candidates, last)
        if len(fresh) >= count:
            candidates = fresh

    return sample_diverse(candidates, count, MAX_PER_SECTOR)
