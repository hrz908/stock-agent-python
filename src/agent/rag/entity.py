"""查询实体识别：把问题里的股票名称/代码映射成代码集合。

只用于 RAG 命中闸的「实体一致性」校验：问题点名了某家公司，但检索结果全是
别家公司的资料时，不能把别家的数据当作答案（实测问「贵州茅台三季报」会命中
一堆其他公司的三季报点评，重排分同样很高）。

名称表进程内缓存 10 分钟；查询失败返回空集合，校验自动跳过，不影响检索主链路。
文件 ingest 的文档没有 stock_codes，不参与该校验（见 sufficiency_check）。

同一轮里 retrieve / sufficiency_check / fast_plan 会拿同一个（改写后的）问句
反复调用 entity_codes：名称表扫描是 O(名称数) 的 Python 子串匹配，按问句缓存
一个短 TTL 的结果，避免一轮多扫两三遍。
"""
import re
import time

from agent.storage.read import get_read_pool

_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_NAME_TTL = 600.0
# 主数据里存在「五 粮 液」这种带空格的官方简称（新浪源），用户从来不会带空格输入，
# 不归一化就永远匹配不上：实体识别为空 -> 覆盖闸不生效、快路径失效、每轮多调一次小模型
_SPACE_RE = re.compile(r"\s+")


def _normalize_name(text: str) -> str:
    """去掉名称里的空白字符。"""
    return _SPACE_RE.sub("", text or "")


# 简称识别：名称表里是「贵州茅台」，用户习惯说「茅台」，只按全名子串匹配会漏。
# 别名有两个来源，都走「唯一 + 非通用词 + 不是别家全名前缀」的过滤，宁缺勿滥——
# 认错标的比认不出更糟（会把 A 家的资料答给 B 家）。
_NAME_PREFIXES = (
    "中国", "贵州", "深圳", "上海", "北京", "浙江", "江苏", "广州", "山东", "四川",
    "安徽", "河南", "湖北", "湖南", "福建", "天津", "重庆", "河北", "山西", "辽宁",
    "吉林", "黑龙江", "江西", "云南", "广西", "海南", "陕西", "甘肃", "青海", "宁夏",
    "新疆", "内蒙古", "西藏", "香港", "青岛", "厦门", "苏州", "无锡",
)
# 剥完前缀剩下的通用词不收：「中国石油」->「石油」会把板块讨论当成个股
_GENERIC_ALIASES = (
    "石油", "石化", "银行", "证券", "科技", "集团", "股份", "电力", "环保", "能源",
    "地产", "医药", "汽车", "电子", "通信", "材料", "重工", "机械", "钢铁", "有色",
    "煤炭", "化工", "食品", "电器", "传媒", "旅游", "交通", "国际", "发展", "控股",
)
# 人工高频简称 -> 全名：只在全名确实存在时生效，跟随主数据改名/退市，不硬编码代码
_ALIAS_NAMES = {
    "茅台": "贵州茅台",
    "招行": "招商银行",
    "东财": "东方财富",
    "隆基": "隆基绿能",
    "中芯": "中芯国际",
    "药明": "药明康德",
    "海康": "海康威视",
    "讯飞": "科大讯飞",
    "宁德": "宁德时代",
    "美的": "美的集团",
    "格力": "格力电器",
    "紫金": "紫金矿业",
    "长电": "长江电力",
    "中际": "中际旭创",
}


def build_name_index(rows) -> list[tuple[str, str]]:
    """[(名称或简称, 代码)]，按长度降序（长名优先，避免短名误命中）。

    rows 为 (code, name)。简称要同时满足三条才收：只对应一只票、不在通用词表里、
    不是别家公司全名的前缀（否则「长电科技」会被「长电」抢走长江电力）。
    """
    full: list[tuple[str, str]] = []
    for code, name in rows:
        normalized = _normalize_name(str(name or ""))
        if normalized:
            full.append((normalized, str(code)))
    known = {name for name, _ in full}

    buckets: dict[str, set[str]] = {}
    for name, code in full:
        for prefix in _NAME_PREFIXES:
            if name.startswith(prefix) and len(name) - len(prefix) >= 2:
                alias = name[len(prefix):]
                if alias not in _GENERIC_ALIASES:
                    buckets.setdefault(alias, set()).add(code)
        for alias, target in _ALIAS_NAMES.items():
            if name == target:
                buckets.setdefault(alias, set()).add(code)

    aliases: list[tuple[str, str]] = []
    for alias, codes in buckets.items():
        if len(codes) != 1 or alias in known:
            continue
        code = next(iter(codes))
        # 别家全名以该别名开头（隆基 -> 隆基机械、长电 -> 长电科技）就放弃：
        # 问「长电科技」时两家都会命中，等于认错标的
        if any(
            other_name.startswith(alias) and other_code != code
            for other_name, other_code in full
        ):
            continue
        aliases.append((alias, code))

    return sorted(full + aliases, key=lambda x: -len(x[0]))

_names_cache: list[tuple[str, str]] = []
_names_at = 0.0

# 问句 -> (写入时刻, 按出现顺序的代码列表)
_mentions_cache: dict[str, tuple[float, list[str]]] = {}
_MENTION_TTL = 60.0
_MENTION_MAX = 256



async def _stock_names() -> list[tuple[str, str]]:
    """[(name, code)]，按名称长度降序（长名优先，避免短名误命中）。"""
    global _names_cache, _names_at
    if _names_cache and time.monotonic() - _names_at < _NAME_TTL:
        return _names_cache
    try:
        pool = await get_read_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT code, name FROM stock_basic "
                    "WHERE status = 'listed' AND length(name) >= 3"
                )
                rows = await cur.fetchall()
        _names_cache = build_name_index(rows)
        _names_at = time.monotonic()
    except Exception:  # noqa: BLE001 - 名称表不可用时不做实体校验
        _names_cache = []
    return _names_cache


async def entity_mentions(query: str) -> list[str]:
    """按出现顺序返回问题里点名的股票代码（去重）。

    顺序对对比类问句有意义：先提到的那只是「本股」，后面的是同行。
    """
    q = (query or "").strip()
    if not q:
        return []
    now = time.monotonic()
    hit = _mentions_cache.get(q)
    if hit and now - hit[0] < _MENTION_TTL:
        return list(hit[1])
    hits = [(m.start(), m.group(1)) for m in _CODE_RE.finditer(q)]
    # 名称表已去空格；问句这边先按原样找（位置才准），找不到再去掉空格找一次：
    # 用户可能写「五粮液」，也可能照抄成「五 粮 液」
    qn = _normalize_name(q)
    for name, code in await _stock_names():
        idx = q.find(name)
        if idx < 0:
            idx = qn.find(name)
        if idx >= 0:
            hits.append((idx, code))
    hits.sort(key=lambda x: x[0])
    out: list[str] = []
    for _, code in hits:
        if code not in out:
            out.append(code)
    if len(_mentions_cache) >= _MENTION_MAX:
        for k in list(_mentions_cache)[: _MENTION_MAX // 4]:
            _mentions_cache.pop(k, None)
    _mentions_cache[q] = (now, out)
    return list(out)


async def entity_codes(query: str) -> set[str]:
    """问题里明确出现的股票代码/全称 -> 代码集合；识别不出返回空集。"""
    return set(await entity_mentions(query))
