"""中文问句的轻量关键词判定：数据类 / 实时类 / 文档类 / 概念类。

这些判定只用于「确定性快路径」：命中实时数据词直接进取数子图，命中文档词
仍走 RAG 漏斗，命中概念词则保持知识型回答。它们不替代 LLM 分类，只负责把
最容易判错、也最不该白跑检索的问句提前分流。
"""
import re

# 数据类关键词：中文/混合词直接子串匹配（大小写不敏感，先 lower 再比）
DATA_KEYWORDS_SUBSTR = (
    # 行情 / 价格类（含「成交量」一族）；查询先 lower 再匹配，故「日K/周K/月K」写成小写形式
    "行情", "报价", "价格", "股价", "多少钱", "啥价", "涨跌", "涨幅", "收盘", "开盘",
    "最高", "最低", "k线", "日k", "周k", "月k", "年k", "走势",
    "成交量", "成交额", "换手", "量比", "振幅",
    # 估值 / 财务类
    "估值", "市盈率", "市净率", "市销率", "股息", "分红", "派息", "市值", "peg",
    "财报", "业绩", "营收", "净利润", "毛利", "净利", "roe", "eps",
    # 资金 / 榜单 / 同业对比 / 资讯类
    "资金", "主力", "龙虎榜", "流入", "流出",
    "同业", "同行", "竞品", "对比", "新闻", "动态", "消息",
)

# 文档型关键词：这些问题更可能由年报/公告/研报回答，仍应先走 RAG。
# 注意与实时类分开：同样是「数据」，行情要取数，年报内容要检索。
DOCUMENT_KEYWORDS_SUBSTR = (
    "财报", "业绩", "营收", "净利润", "毛利", "净利",
    "年报", "季报", "中报", "研报", "公告", "招股书", "定期报告",
)

# 实时类关键词：取数工具能直接给出数值，RAG 语料通常没有当日数据。
# 与 DATA_KEYWORDS_SUBSTR 的差集 = 去掉文档型，保留 roe/eps 这类当前指标。
REALTIME_KEYWORDS_SUBSTR = (
    "行情", "报价", "价格", "股价", "多少钱", "啥价", "涨跌", "涨幅", "收盘", "开盘",
    "最高", "最低", "k线", "日k", "周k", "月k", "年k", "走势",
    "成交量", "成交额", "换手", "量比", "振幅",
    "估值", "市盈率", "市净率", "市销率", "股息", "分红", "派息", "市值", "peg",
    "roe", "eps",
    "资金", "主力", "龙虎榜", "流入", "流出",
    "同业", "同行", "竞品", "对比", "新闻", "动态", "消息",
)

# 短英文指标整词匹配（词边界），防止 "apple" 命中 "pe"、"space" 命中 "ps"
DATA_KEYWORDS_WORD = re.compile(r"(?<![a-zA-Z])(pe|pb|ps)(?![a-zA-Z])", re.IGNORECASE)

# 结构化财务数据问句：问的是可被取数工具直接给出的数值/指标，
# 即使 classify 判成文档型 stock，主图也应改路由到取数子图（工具 + 财报卡）。
# 原因：语料扩充后 RAG 能命中财报原文，若仍走 RAG 会绕过结构化工具与卡片（实测 F-01）。
STRUCTURED_DATA_KEYWORDS = (
    "财务指标", "主要指标", "财报数据", "业绩数据", "营收数据", "净利润数据",
    "增长情况", "同比", "环比",
)

# 概念型问句：问「是什么 / 怎么算 / 有什么区别」，不需要实时数值。
CONCEPT_KEYWORDS = (
    "解释", "含义", "意思", "区别", "不同", "为什么", "怎么算", "如何算",
    "怎么用", "如何用", "是什么", "什么是", "通俗", "简单说", "怎么理解",
    "如何理解", "讲讲", "介绍", "科普", "定义", "有什么区别",
)


def hit_data_keyword(query: str) -> bool:
    """数据类问句 -> True；短英文指标用词边界，中文/混合词用子串。"""
    q = (query or "").lower()
    if any(k in q for k in DATA_KEYWORDS_SUBSTR):
        return True
    return bool(DATA_KEYWORDS_WORD.search(q))


def is_document_question(text: str) -> bool:
    """问题指向年报/公告/研报等内容 -> True（仍走 RAG）。"""
    q = (text or "").lower()
    return any(k in q for k in DOCUMENT_KEYWORDS_SUBSTR)


def is_realtime_data_question(text: str) -> bool:
    """实时数值类问句 -> True（可直接进取数子图，跳过 RAG）。

    文档型词优先：像「财报说了什么」这类问题即使含「财报」，也应先检索原文，
    不能因为它是「数据」就直接取数。
    """
    q = (text or "").lower()
    if is_document_question(q):
        return False
    if any(k in q for k in REALTIME_KEYWORDS_SUBSTR):
        return True
    return bool(DATA_KEYWORDS_WORD.search(q))


def is_structured_data_question(text: str) -> bool:
    """问的是可被取数工具直接给出的结构化财务数值/指标 -> True。

    与 is_document_question 的分工：问「财报说了什么」走 RAG；
    问「主要财务指标 / 营收增长情况 / 同比多少」走取数工具并出财报卡。
    """
    q = (text or "").lower()
    return any(k in q for k in STRUCTURED_DATA_KEYWORDS)


def is_concept_question(text: str) -> bool:
    """纯概念/知识型问题 -> True（不需要实时行情数据）。"""
    return any(k in (text or "") for k in CONCEPT_KEYWORDS)


def fast_intent(text: str) -> str:
    """确定性快路径：命中实时数据词 -> stock_agent；文档/混合数据词 -> stock。

    返回空串表示没有命中关键词，交给路由 LLM。classify 与多轮合并路由共用这一口径，
    避免「合并路由把取数问句判回 RAG」造成必然失败的检索空转。
    """
    q = (text or "").strip()
    if not q or not hit_data_keyword(q):
        return ""
    if is_realtime_data_question(q) and not is_concept_question(q):
        return "stock_agent"
    return "stock"
