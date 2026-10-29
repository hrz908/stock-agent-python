"""Quick Ask 候选池。

候选池只存 Redis、不落库：热度分是 10 分钟量级的中间结果，落库既多一张表，
又要额外处理「上游没数」时的脏读。

打分只统计网关真正拿得到的信号（涨跌幅偏离 / 成交额 / 换手率 / 站内行为）。
新闻热度、板块资金流、龙虎榜、北向持仓在网关没有数据源，权重不按 0 计入，
而是把剩余权重按比例摊给可用信号——否则整池分数被系统性压平，
「热度加权」会退化成「接近均匀随机」。

进池后再按「有没有文本语料」收一道：推荐出去的问题要能被答出来，
用户点开建议才不会撞上「不在当前的文本语料范围内」那句话术。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import UTC, datetime, timedelta, timezone

import httpx

from gateway.adapters.quote.batch import fetch_details
from gateway.domain.stockcode import market_of, normalize_code
from gateway.infra.deps import runtime
from gateway.services.quickask.models import Candidate, MarketMeta
from gateway.services.quickask.template import digits_of

logger = logging.getLogger("stocka.gateway.quickask.pool")

POOL_KEY = "stock:hot:pool"  # ZSET：score = 热度分，member = 5 段股票信息
POOL_TEMP_PREFIX = "stock:hot:pool:tmp:"  # 重建期间的临时键，写完整批后 RENAME 原子替换
ASK_HEAT_KEY = "stock:ask:heat"  # ZSET：站内 Quick Ask 点击次数
POOL_TTL = 10 * 60
ASK_HEAT_TTL = 7 * 24 * 3600

MAX_POOL_SIZE = 200  # 候选池上限：再大就会混进没人认识的冷门票
MAX_UNIVERSE = 200  # 参与打分的股票上限：200 只 ≈ 10 次批量行情请求
# 语料过滤后至少留下这么多只：少于这个数更可能是语料库还没铺开，
# 而不是这批票不行，退回未过滤的池子——候选池宁可宽，也不能空。
CORPUS_MIN_KEEP = 40

WEIGHT_CHANGE_PCT = 0.25
WEIGHT_AMOUNT = 0.20
WEIGHT_TURNOVER = 0.15
WEIGHT_BEHAVIOR = 0.15
WEIGHT_AVAILABLE = WEIGHT_CHANGE_PCT + WEIGHT_AMOUNT + WEIGHT_TURNOVER + WEIGHT_BEHAVIOR

SINA_RANK_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData?page=%d&num=100&sort=amount&asc=0&node=hs_a"
)
SINA_MAX_BYTES = 2 << 20
SINA_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
CST = timezone(timedelta(hours=8))

# evergreen 冷启动与保底清单：候选池为空（凌晨、主数据未同步、Redis 挂了）时
# 也要有像样的股票可问。同时它给零行为数据的新站一个最低热度，
# 避免「全市场按热度排序」在数据稀疏时退化成按代码排序。
EVERGREEN: list[Candidate] = [
    Candidate("SH600519", "贵州茅台", "食品饮料"),
    Candidate("SZ300750", "宁德时代", "电力设备"),
    Candidate("SZ002594", "比亚迪", "汽车"),
    Candidate("SH600036", "招商银行", "银行"),
    Candidate("SZ300059", "东方财富", "非银金融"),
    Candidate("SZ300308", "中际旭创", "通信"),
    Candidate("SH601318", "中国平安", "非银金融"),
    Candidate("SZ000858", "五粮液", "食品饮料"),
    Candidate("SH600900", "长江电力", "公用事业"),
    Candidate("SH601899", "紫金矿业", "有色金属"),
    Candidate("SZ002475", "立讯精密", "电子"),
    Candidate("SZ000333", "美的集团", "家用电器"),
    Candidate("SH688981", "中芯国际", "电子"),
    Candidate("SH601012", "隆基绿能", "电力设备"),
    Candidate("SH600030", "中信证券", "非银金融"),
    Candidate("SZ002415", "海康威视", "计算机"),
    Candidate("SH603259", "药明康德", "医药生物"),
    Candidate("SZ000001", "平安银行", "银行"),
    Candidate("SH601088", "中国神华", "煤炭"),
    Candidate("SH600276", "恒瑞医药", "医药生物"),
    Candidate("SZ000651", "格力电器", "家用电器"),
    Candidate("SH601857", "中国石油", "石油石化"),
    Candidate("SH600887", "伊利股份", "食品饮料"),
    Candidate("SZ002230", "科大讯飞", "计算机"),
    Candidate("SH601668", "中国建筑", "建筑装饰"),
    Candidate("SZ300124", "汇川技术", "机械设备"),
    Candidate("SH600585", "海螺水泥", "建筑材料"),
    Candidate("SZ002304", "洋河股份", "食品饮料"),
]

# 防同进程并发重建：池子过期那一瞬间涌进来的请求不该各建一遍
_build_lock = asyncio.Lock()
# 仅在 Redis 不可用时兜底，行为与 ZSET 一致但不跨实例
_mem_pool: list[Candidate] = []
_mem_at: float = 0.0


def now_cst() -> datetime:
    return datetime.now(UTC).astimezone(CST)


def evergreen_pool() -> list[Candidate]:
    return [
        Candidate(code=c.code, name=c.name, sector=c.sector, score=100.0)
        for c in EVERGREEN
    ]


# ---------------------------------------------------------------- 池子读写


async def read_pool() -> list[Candidate]:
    if not _redis_ready():
        return []
    try:
        rows = await runtime().redis.zrange(
            POOL_KEY, 0, MAX_POOL_SIZE - 1, desc=True, withscores=True
        )
    except Exception as exc:  # noqa: BLE001 - Redis 抖动不该让首屏建议整体消失
        logger.info("read hot pool failed: %s", exc)
        return []

    out: list[Candidate] = []
    for member, score in rows:
        candidate = Candidate.decode(str(member), float(score))
        if candidate is not None:
            out.append(candidate)
    return out


async def write_pool(pool: list[Candidate]) -> None:
    if not _redis_ready() or not pool:
        return
    tmp = POOL_TEMP_PREFIX + str(time.time_ns())
    try:
        pipe = runtime().redis.pipeline(transaction=False)
        pipe.delete(tmp)
        for candidate in pool:
            pipe.zadd(tmp, {candidate.encode(): candidate.score})
        pipe.expire(tmp, POOL_TTL)
        # RENAME 原子替换：读方不会撞上「旧池已删、新池没写完」的空窗
        pipe.rename(tmp, POOL_KEY)
        await pipe.execute()
    except Exception as exc:  # noqa: BLE001
        logger.info("write hot pool failed: %s", exc)


def _redis_ready() -> bool:
    try:
        return runtime().redis_ok
    except RuntimeError:
        return False


async def load_pool() -> tuple[list[Candidate], str]:
    """读候选池；为空（过期 / 首次 / Redis 被清）时惰性重建并回写。

    仓库约定不使用 cron，重建只能由请求触发；代价被 TTL 摊平，
    10 分钟内只有第一个请求变慢。
    """
    global _mem_pool, _mem_at

    pool = await read_pool()
    if pool:
        return pool, "redis"

    async with _build_lock:
        # 等锁期间别的请求可能已经建好，再读一次，避免同一轮重复打上游
        pool = await read_pool()
        if pool:
            return pool, "redis"

        # Redis 不可用时用进程内缓存兜住 TTL，否则每个首屏请求都重新拉一轮行情
        if not _redis_ready() and _mem_pool and time.monotonic() - _mem_at < POOL_TTL:
            return _mem_pool, "memory"

        built = await build_pool()
        if not built:
            return evergreen_pool(), "fallback"

        if not _redis_ready():
            _mem_pool, _mem_at = built, time.monotonic()
        await write_pool(built)
        return built, "built"


# ---------------------------------------------------------------- 建池


async def build_pool() -> list[Candidate]:
    universe, heat = await load_universe()
    if not universe:
        return []

    details = await fetch_details([candidate.code for candidate in universe])
    if not details:
        logger.info("build hot pool: no quote details, universe=%d", len(universe))
        return []

    scored: list[Candidate] = []
    for candidate in universe:
        detail = details.get(candidate.code)
        # 停牌 / 退市 / 上游漏返回：价格为零的数据进池只会生成「现在什么价」这种烂问题
        if detail is None or detail.price == 0:
            continue
        candidate.name = pick_name(candidate.name, detail.name)
        candidate.change_pct = float(detail.change_percent)
        candidate.volume_ratio = float(detail.volume_ratio)
        candidate.amount = float(detail.amount)
        candidate.turnover = float(detail.turnover)
        candidate.heat = heat.get(candidate.code, 0.0)
        candidate.sector = (candidate.sector or "").strip()
        scored.append(candidate)

    scored = keep_answerable(
        scored, await load_corpus_codes([digits_of(c.code) for c in scored])
    )

    score_pool(scored)
    scored.sort(key=lambda c: (-c.score, c.code))
    return scored[:MAX_POOL_SIZE]


async def load_corpus_codes(codes: list[str]) -> set[str] | None:
    """返回这批裸码里「有已入库文档」的子集；判定不可用时返回 None。

    口径必须与 Agent 侧的覆盖闸一致：按 ashare_documents 里真有文档判（has_docs），
    不按 ashare_universe（in_pool）——后者会把「进了采集名单但还没采到」的票误判成
    没语料，反而把能答的问题挡掉。
    查询失败返回 None 而不是空集：空集等于「全部没语料」，一次库抖动就能清空首屏。
    """
    if not codes:
        return set()
    try:
        async with runtime().pg.pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT t.code
                FROM unnest(%s::text[]) AS t(code)
                WHERE EXISTS (
                    SELECT 1 FROM ashare_documents a
                     WHERE a.stock_codes && ARRAY[t.code]::text[]
                )
                """,
                (list(dict.fromkeys(codes)),),
            )
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 - 语料表缺失 / 库抖动都不该让建池失败
        logger.info("load corpus codes failed: %s", exc)
        return None
    return {str(row[0]) for row in rows}


def keep_answerable(pool: list[Candidate], covered: set[str] | None) -> list[Candidate]:
    """给候选打上 corpus 标记，并尽量只保留有语料的那些。

    候选池是按成交额榜实时建的，语料库是每天收盘后按另一份 Top 300 名单采的，
    两者并不重合（盘中突然放量的票、北交所小票都可能没有语料）。
    留着标记而不是只留过滤结果：过滤会触发保底（见 CORPUS_MIN_KEEP），
    保底放回来的那些票仍然知道自己没语料，模板层会避开答不了的问句。
    """
    for candidate in pool:
        candidate.corpus = covered is None or digits_of(candidate.code) in covered
    answerable = [candidate for candidate in pool if candidate.corpus]
    if len(answerable) >= CORPUS_MIN_KEEP:
        return answerable
    logger.info(
        "corpus filter skipped: answerable=%d < %d, keep whole pool=%d",
        len(answerable), CORPUS_MIN_KEEP, len(pool),
    )
    return pool


async def load_universe() -> tuple[list[Candidate], dict[str, float]]:
    """组装打分前的候选清单：成交额榜前 200 为主路，榜单不可用时退回「站内自选 + 常青股」。

    绝不能「按代码顺序取前 200」：零热度的排序会退化成字典序，BJ920xxx 整段排在最前，
    候选池会被北交所小票占满（+30% 的陌生票直接进首屏）。
    """
    heat = await load_ask_heat()
    for code, count in (await load_watchlist_heat()).items():
        heat[code] = heat.get(code, 0.0) + count

    meta = await load_meta()
    moment = now_cst()

    ranked = await load_rank_universe()
    if len(ranked) < 20:
        # 榜单抓不到（网络 / 接口改版）：只用有人关注过的 + 常青股，
        # 宁可池子小，也不要拿零热度的陌生代码凑数
        ranked = watchlist_universe(meta, heat)

    out: list[Candidate] = []
    for candidate in ranked:
        row = meta.get(candidate.code)
        if row is not None:
            # ST / 已退市硬过滤：让用户点开一支 ST 股的「风险点」没有产品价值
            if row.is_st or row.status != "listed" or fresh_listing(row.list_date, moment):
                continue
            candidate.name = pick_name(row.name, candidate.name)
            candidate.sector = (row.sector or "").strip()
            # 除权除息临时简称（XD/XR/DR）不进池：文案会变成「XD澜起科现在什么价？」
            if ex_rights_marked(candidate.name):
                continue
        elif candidate.code.startswith("BJ"):
            # 榜单本身不含北交所；没有主数据佐证时不要把 BJ 小票放进来
            continue
        candidate.heat = heat.get(candidate.code, 0.0)
        out.append(candidate)

    return out[:MAX_UNIVERSE], heat


def watchlist_universe(
    meta: dict[str, MarketMeta], heat: dict[str, float]
) -> list[Candidate]:
    """榜单失败时的保底：常青股 + 有行为热度的股票。

    不掺零热度的陌生代码——按代码补满 200 只会把北交所小票顶到前面。
    """
    out: list[Candidate] = []
    seen: set[str] = set()

    def add(code: str, name: str, sector: str, value: float) -> None:
        if code in seen:
            return
        row = meta.get(code)
        if row is not None:
            if row.status != "listed" or row.is_st:
                return
            name = pick_name(row.name, name)
            sector = pick_name(sector, (row.sector or "").strip())
        if not name or ex_rights_marked(name):
            return
        seen.add(code)
        out.append(Candidate(code=code, name=name, sector=sector, heat=value))

    for candidate in EVERGREEN:
        add(candidate.code, candidate.name, candidate.sector, 1 + heat.get(candidate.code, 0.0))
    for code, value in heat.items():
        if value < 1:
            continue
        add(code, "", "", value)

    out.sort(key=lambda c: (-c.heat, c.code))
    return out[:MAX_UNIVERSE]


# ---------------------------------------------------------------- 打分


def score_pool(pool: list[Candidate]) -> None:
    """池内相对归一化：涨跌幅取「偏离池均值」的绝对值，成交额/换手率/热度取分位。

    绝对阈值在牛熊市里都会失真，相对排序更稳。
    """
    n = len(pool)
    if n == 0:
        return

    mean = sum(candidate.change_pct for candidate in pool) / n
    deviations = [abs(candidate.change_pct - mean) for candidate in pool]
    max_dev = max(deviations)

    amount_pct = percentile(pool, lambda c: c.amount)
    turnover_pct = percentile(pool, lambda c: c.turnover)
    heat_pct = percentile(pool, lambda c: c.heat)

    for index, candidate in enumerate(pool):
        dev = deviations[index] / max_dev if max_dev > 0 else 0.0
        raw = (
            dev * WEIGHT_CHANGE_PCT
            + amount_pct[index] * WEIGHT_AMOUNT
            + turnover_pct[index] * WEIGHT_TURNOVER
            + heat_pct[index] * WEIGHT_BEHAVIOR
        )
        # 折算到 0~100，与设计文档 hot_score 口径一致，排查时可直接看
        candidate.score = _round_half_away(raw / WEIGHT_AVAILABLE * 10000) / 100


def percentile(pool: list[Candidate], value) -> list[float]:
    """返回每个位置的分位值（0~1）：并列值取相同分位，池内最小 0、最大 1。"""
    n = len(pool)
    out = [0.0] * n
    if n == 1:
        out[0] = 1.0
        return out

    order = sorted(range(n), key=lambda i: value(pool[i]))
    prev_value = 0.0
    prev_rank = 0.0
    for rank, index in enumerate(order):
        current = value(pool[index])
        if rank > 0 and current == prev_value:
            out[index] = prev_rank
            continue
        out[index] = rank / (n - 1)
        prev_value, prev_rank = current, out[index]
    return out


def _round_half_away(value: float) -> float:
    """对齐 Go 的 math.Round（四舍五入远离零），避免 Python 银行家舍入。"""
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


# ---------------------------------------------------------------- 站内行为信号


async def bump_ask_heat(code: str) -> None:
    """记录一次 Quick Ask 点击：这是「站内行为」这一档权重唯一的来源。"""
    if not _redis_ready():
        return
    try:
        pipe = runtime().redis.pipeline(transaction=False)
        pipe.zincrby(ASK_HEAT_KEY, 1, code)
        pipe.expire(ASK_HEAT_KEY, ASK_HEAT_TTL)
        await pipe.execute()
    except Exception as exc:  # noqa: BLE001
        logger.info("bump ask heat failed: code=%s err=%s", code, exc)


async def load_ask_heat() -> dict[str, float]:
    out: dict[str, float] = {}
    if not _redis_ready():
        return out
    try:
        rows = await runtime().redis.zrange(
            ASK_HEAT_KEY, 0, MAX_UNIVERSE - 1, desc=True, withscores=True
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("load ask heat failed: %s", exc)
        return out
    for member, score in rows:
        out[str(member)] = float(score)
    return out


async def load_watchlist_heat() -> dict[str, float]:
    """用全站自选加入数当「关注度」代理：比搜索稀疏，但不需要埋点表。"""
    out: dict[str, float] = {}
    try:
        async with runtime().pg.pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT stock_code, COUNT(*) AS cnt
                FROM user_watchlist
                WHERE enabled = TRUE
                GROUP BY stock_code
                ORDER BY cnt DESC
                LIMIT %s
                """,
                (MAX_UNIVERSE,),
            )
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 - 自选表缺失也不能让首屏整体失败
        logger.info("load watchlist heat failed: %s", exc)
        return out
    for row in rows:
        normalized, ok = normalize_code(row[0])
        if ok:
            out[normalized] = float(row[1])
    return out


async def load_meta() -> dict[str, MarketMeta]:
    """读主数据：名称 / 行业 / 上市日期 / ST。

    行业口径不绑 std：生产上东财接口被封，sync-master 落的是证监会口径（std='sina'），
    绑 'em' 会让板块全空、同板块约束直接失效。
    """
    out: dict[str, MarketMeta] = {}
    try:
        async with runtime().pg.pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT b.code,
                       b.name,
                       COALESCE(i.l1, '') AS sector,
                       COALESCE(to_char(b.list_date, 'YYYY-MM-DD'), '') AS list_date,
                       b.status,
                       b.is_st
                FROM stock_basic b
                LEFT JOIN LATERAL (
                  SELECT l1 FROM stock_industry
                  WHERE code = b.code
                  ORDER BY (std = 'em') DESC, updated_at DESC
                  LIMIT 1
                ) i ON TRUE
                """
            )
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 - 主数据是增强项，不是前置依赖
        logger.info("load stock meta failed: %s", exc)
        return out

    for row in rows:
        market = market_of(row[0] or "")
        if not market:
            continue
        out[market + row[0]] = MarketMeta(
            code=row[0],
            name=row[1] or "",
            sector=row[2] or "",
            list_date=row[3] or "",
            status=row[4] or "",
            is_st=bool(row[5]),
        )
    return out


# ---------------------------------------------------------------- 成交额榜


async def load_rank_universe() -> list[Candidate]:
    """取成交额前 200（2 页 × 100）。任何一页失败就用手上已有的。"""
    out: list[Candidate] = []
    seen: set[str] = set()

    for page in (1, 2):
        try:
            items = await _fetch_rank_page(page)
        except Exception as exc:  # noqa: BLE001
            logger.info("fetch rank page %d failed: %s", page, exc)
            break
        for item in items:
            code, ok = normalize_code(str(item.get("symbol") or ""))
            name = str(item.get("name") or "")
            if not ok or not name or code in seen:
                continue
            seen.add(code)
            out.append(
                Candidate(
                    code=code,
                    name=name,
                    change_pct=_as_float(item.get("changepercent")),
                    # 元 → 万元，与腾讯 amount 口径对齐，否则同池内分位计算会串
                    amount=_as_float(item.get("amount")) / 1e4,
                    turnover=_as_float(item.get("turnoverratio")),
                )
            )
            if len(out) >= MAX_UNIVERSE:
                return out
    return out


async def _fetch_rank_page(page: int) -> list[dict]:
    # 榜单接口偶尔慢，单独用 8s 超时，不拖住其它模块的默认档
    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.get(
            SINA_RANK_URL % page,
            # 新浪榜单对无 Referer 的请求会返回空数组，UA 也必须是浏览器口径
            headers={"User-Agent": SINA_UA, "Referer": "https://finance.sina.com.cn"},
        )
    if response.status_code != 200:
        raise RuntimeError(f"unexpected status {response.status_code}")

    import json

    body = response.content[:SINA_MAX_BYTES]
    payload = json.loads(body.decode("utf-8", "replace"))
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- 小工具


def fresh_listing(list_date: str, moment: datetime) -> bool:
    """上市不足 7 个自然日视为新股：没有足够历史量价，模板问的形态/资金都答不出来。

    用自然日近似交易日，省掉节假日日历。
    """
    if not list_date:
        return False
    try:
        listed = datetime.strptime(list_date, "%Y-%m-%d").replace(tzinfo=CST)
    except ValueError:
        return False
    return (moment - listed) < timedelta(days=7)


def pick_name(current: str, fallback: str) -> str:
    text = (current or "").strip()
    return text if text else (fallback or "").strip()


def ex_rights_marked(name: str) -> bool:
    """除权除息日交易所给简称加的临时前缀（XD / XR / DR），且往往伴随截断。

    这种名字拼进问句就是「XD澜起科现在什么价？」，用户按正式简称提问也匹配不到实体，
    所以不进候选池：等主数据换回正式简称（下一次同步）自然会回来。
    """
    upper = (name or "").strip().upper()
    return any(upper.startswith(prefix) for prefix in ("XD", "XR", "DR"))
