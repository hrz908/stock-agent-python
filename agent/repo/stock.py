"""股票主数据查询（迁移 008 的 stock_basic / stock_industry）。

只做 agent 侧真正需要的一件事：给同业对比补候选标的。
代码格式固定 6 位裸码（与 ashare_documents.stock_codes 一致），转换由调用方负责。
"""
from db import fetch_all

# 单次返回的候选上限：工具层也有一份，两边各自收紧，放宽时不会只改一处就失控
_MAX_PEERS = 20
_DEFAULT_LIMIT = 6


def _clean_code(code) -> str:
    """只放行 6 位裸码：格式转换是调用方的事，写错了就该立刻回空，不去表里猜。"""
    c = str(code or "").strip()
    return c if len(c) == 6 and c.isdigit() else ""


def _clamp_limit(limit) -> int:
    """limit 收敛到 [1, _MAX_PEERS]；None / 非数字一律回落默认值，不把异常抛给工具层。"""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = _DEFAULT_LIMIT
    return max(1, min(n, _MAX_PEERS))


# 行业按「东财优先、否则最新一条」取：行业主源是东财（std='em'），
# 东财被封时 ashare 会退新浪（std='sina'），写死 em 会让同业工具整片空掉
_INDUSTRY_LATERAL = """
LATERAL (
  SELECT l1, l2, l3 FROM stock_industry
  WHERE code = b.code
  ORDER BY (std = 'em') DESC, updated_at DESC
  LIMIT 1
) i ON TRUE
"""

# 本股 + 行业：找不到股票或没有行业时后续查询自然返回空，不用异常表达「没数据」
_ME_SQL = f"""
SELECT b.code, b.name, b.market,
       COALESCE(i.l1, '') AS industry_l1,
       COALESCE(i.l2, '') AS industry_l2,
       COALESCE(i.l3, '') AS industry_l3
FROM stock_basic b
LEFT JOIN {_INDUSTRY_LATERAL}
WHERE b.code = %(code)s
"""

# 同行业候选：默认排除 ST（同业对比里放 ST 股会误导）。
# 分层排序是关键：只按一级分组时「食品饮料」里会混进黑芝麻、广弘控股，
# 白酒的同行要先被 白酒(l3) > 饮料(l2) > 食品饮料(l1) 顶上来，所以 tier 第一顺位。
_PEERS_SQL = f"""
SELECT b.code, b.name, b.market,
       CASE
         WHEN %(l3)s <> '' AND i.l3 = %(l3)s THEN 1
         WHEN %(l2)s <> '' AND i.l2 = %(l2)s THEN 2
         ELSE 3
       END AS tier
FROM stock_basic b
JOIN {_INDUSTRY_LATERAL}
WHERE i.l1 = %(l1)s
  AND b.code <> %(code)s
  AND b.status = 'listed'
  AND (%(include_st)s OR NOT b.is_st)
ORDER BY tier, b.is_st, b.code
LIMIT %(limit)s
"""


async def industry_peers(code: str, limit: int = 6, include_st: bool = False) -> dict:
    """同行业候选（同 l1 内按 l3 > l2 > l1 分层，像白酒的同行排在饮料前面）。

    返回 {"code","name","market","industry","industry_detail","peers":[{code,name,market}]}；
    industry 是一级行业，industry_detail 是「食品饮料-饮料-白酒」这样的完整路径；
    股票不在表里、或它还没有行业时 peers 为空——这是数据状态，不是错误；
    入参不是 6 位裸码时同样返回空结构且不查库——那是调用方的 bug，不该伪装成「没数据」。
    """
    me_code = _clean_code(code)
    if not me_code:
        return {"code": "", "name": "", "market": "", "industry": "",
                "industry_detail": "", "peers": []}

    rows = await fetch_all(_ME_SQL, {"code": me_code})
    if not rows:
        return {"code": me_code, "name": "", "market": "", "industry": "",
                "industry_detail": "", "peers": []}

    me = rows[0]
    l1 = me.get("industry_l1") or ""
    l2 = me.get("industry_l2") or ""
    l3 = me.get("industry_l3") or ""
    peers: list[dict] = []
    if l1:
        fresh = await fetch_all(_PEERS_SQL, {
            "l1": l1,
            "l2": l2,
            "l3": l3,
            "code": me_code,
            # 只认布尔真值：字符串 "false" 经 bool() 会变 True，psycopg 也不收字符串
            "include_st": include_st is True,
            "limit": _clamp_limit(limit),
        })
        peers = [{"code": r["code"], "name": r["name"], "market": r["market"]} for r in fresh]

    return {
        "code": me["code"],
        "name": me["name"],
        "market": me["market"],
        "industry": l1,
        "industry_detail": "-".join(p for p in (l1, l2, l3) if p),
        "peers": peers,
    }
