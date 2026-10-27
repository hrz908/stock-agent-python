"""语料覆盖判定（进程内缓存）。

用途只有一个：区分「检索没找好」和「这只票根本没被采集」。后者不该升级到取数 agent
去自由发挥——那会把没有来源的常识混进带引用的回答里，用户分不清哪句有资料支撑。

缓存 10 分钟：覆盖情况只在每天采集（15:10）后才可能变，而同一只标的会被反复问到
（追问、重试、多轮）。查询异常返回空字典，调用方据此跳过判定——
宁可让检索照常跑完，也不要因为一次库抖动误报「没覆盖」，把能答的问题挡掉。
"""
import time

_TTL = 600.0
_CACHE_MAX = 2048

# code -> (过期时刻, has_docs, in_pool)
_cache: dict[str, tuple[float, bool, bool]] = {}


def _get(code: str, now: float) -> tuple[bool, bool] | None:
    item = _cache.get(code)
    if item and item[0] > now:
        return item[1], item[2]
    return None


async def detail(codes) -> dict[str, tuple[bool, bool]]:
    """code -> (has_docs, in_pool)。查询失败返回 {}（调用方按「不拦」处理）。"""
    ordered = list(dict.fromkeys(codes or []))
    if not ordered:
        return {}
    now = time.monotonic()
    unknown = [c for c in ordered if _get(c, now) is None]
    if unknown:
        try:
            from agent.repo.corpus import coverage_of

            rows = await coverage_of(unknown)
        except Exception:  # noqa: BLE001 - 判定不可用时不改变原行为
            return {}
        for row in rows:
            code = str(row["code"])
            _cache[code] = (now + _TTL, bool(row["has_docs"]), bool(row["in_pool"]))
        if len(_cache) > _CACHE_MAX:
            for key in list(_cache)[: _CACHE_MAX // 4]:
                _cache.pop(key, None)
    return {c: item for c in ordered if (item := _get(c, now)) is not None}
