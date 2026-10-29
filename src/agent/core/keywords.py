"""中文问句的轻量关键词判定：数据类 / 实时类 / 文档类 / 概念类。

这些判定只用于「确定性快路径」：命中实时数据词直接进取数子图，命中文档词
仍走 RAG 漏斗，命中概念词则保持知识型回答。它们不替代 LLM 分类，只负责把
最容易判错、也最不该白跑检索的问句提前分流。
"""
import re

# 数据类关键词：中文/混合词直接子串匹配（大小写不敏感，先 lower 再比）
DATA_KEYWORDS_SUBSTR = (
    # 行情 / 价格类（含「成交量」一族）；查询先 lower 再匹配，故「日K/周K/月K」写成小写形式
    # 「什么价」「价位」「现价」是最常见的口语问法（首屏引导问题也用它），
    # 缺了它们只能交给路由 LLM，实测会被判成闲聊 → 答「没有行情接口」
    "行情", "报价", "价格", "股价", "现价", "什么价", "多少钱", "啥价", "价位",
    "涨跌", "涨幅", "收盘", "开盘",
    "最高", "最低", "k线", "日k", "周k", "月k", "年k", "走势",
    "成交量", "成交额", "换手", "量比", "振幅",
    # 估值 / 财务类
    "估值", "市盈率", "市净率", "市销率", "股息", "分红", "派息", "市值", "peg",
    "财报", "业绩", "营收", "净利润", "毛利", "净利", "roe", "eps",
    # 资金 / 榜单 / 同业对比 / 资讯类
    "资金", "主力", "龙虎榜", "流入", "流出",
    "同业", "同行", "竞品", "对比", "新闻", "动态", "消息",
    # 机构观点类：没有快路径时同一句「最新机构研报观点」会被路由 LLM 时而判
    # clarify 反问、时而判 stock（实测两种都出现过），补成确定性文档型问句。
    # 只收「研报观点 / 机构研报」这类明确指向观点内容的组合词，不收「研报」本身：
    # 它属于文档型问句（见 DOCUMENT_KEYWORDS_SUBSTR），不该被当成取数关键词
    "评级", "目标价", "盈利预测", "机构观点", "研报观点", "机构研报",
)

# 文档型关键词：这些问题更可能由年报/公告/研报回答，仍应先走 RAG。
# 注意与实时类分开：同样是「数据」，行情要取数，年报内容要检索。
DOCUMENT_KEYWORDS_SUBSTR = (
    "财报", "业绩", "营收", "净利润", "毛利", "净利",
    "年报", "季报", "中报", "研报", "公告", "招股书", "定期报告",
    # 机构观点类：答案在研报正文里，先走 RAG；未命中再升级取数（get_research_reports）
    "评级", "目标价", "盈利预测", "机构观点", "研报观点", "机构研报",
)

# 实时类关键词：取数工具能直接给出数值，RAG 语料通常没有当日数据。
# 与 DATA_KEYWORDS_SUBSTR 的差集 = 去掉文档型，保留 roe/eps 这类当前指标。
REALTIME_KEYWORDS_SUBSTR = (
    "行情", "报价", "价格", "股价", "现价", "什么价", "多少钱", "啥价", "价位",
    "涨跌", "涨幅", "收盘", "开盘",
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


# 工具 / 入门类咨询的强特征词：语料库里没有「炒股软件对比」「开户流程」这类文档，
# 进 RAG 只会白跑一次检索（线上实测一次超时 20s）再降级，直接给通用回答更好。
# 只收强特征：孤立的「软件 / 平台 / 券商」不收——"东方财富这个软件怎么样"是问个股。
CONSULT_KEYWORDS = (
    "新手", "入门", "开户", "佣金", "手续费", "印花税",
    "炒股软件", "股票软件", "交易软件", "哪个软件", "什么软件", "用什么软件",
    "哪个券商", "哪家券商", "哪个平台",
)

# 要具体标的的问句：即使带「新手 / 入门」也要走投研链路，不能被当成纯咨询送进闲聊
_STOCK_PICK_KEYWORDS = (
    "买什么", "哪只", "哪支", "几只", "标的", "推荐股", "荐股", "选股", "买入",
)
# 「个股」不能按裸子串匹配：「哪一个股票软件」里的「个股票」会被误伤（实测）
_STOCK_PICK_RE = re.compile(r"个股(?!票)")


def is_consult_question(text: str) -> bool:
    """工具 / 入门类咨询（不指向具体标的）-> True。

    判据刻意保守：带 6 位代码、或含「买什么 / 哪只 / 几只」这类要标的的词，一律不算，
    交给后面的链路按投研问题处理。
    """
    q = (text or "").lower()
    if not q or re.search(r"\d{6}", q):
        return False
    if any(k in q for k in _STOCK_PICK_KEYWORDS) or _STOCK_PICK_RE.search(q):
        return False
    return any(k in q for k in CONSULT_KEYWORDS)


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


# 时效词：出现即说明问的是「当下状态」，不是概念定义
_NOW_WORDS = ("现在", "今天", "今日", "目前", "当前", "此刻", "盘中", "这两天", "近几日", "实时")
_CODE_RE = re.compile(r"\d{6}")


def is_concept_question(text: str) -> bool:
    """纯概念/知识型问题 -> True（不需要实时行情数据）。

    「是什么」这类概念词只是必要条件，不能单独成立：用户问
    「襄阳轴承（000678）现在什么价？今天这波涨跌是什么逻辑」时命中了「是什么」，
    但它要的是这只票当下的数据。实测把它判成概念问题会走通用问答，
    答成「我这边没有实时行情接口」（线上复现）。
    判定：同时命中行情类词，且带 6 位代码或时效词 → 是取数问句，不是概念题。
    """
    q = text or ""
    if not any(k in q for k in CONCEPT_KEYWORDS):
        return False
    if hit_data_keyword(q) and (_CODE_RE.search(q) or any(w in q for w in _NOW_WORDS)):
        return False
    return True


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


# 问候/寒暄词：命中的短句直接判闲聊，不必让路由 LLM 再跑一次
# （线上实测「你好」光分类就 1.81s，占首响 2.37s 的七成）
_GREETING_WORDS = (
    "你好", "您好", "hi", "hello", "hey", "在吗", "在么", "谢谢", "多谢", "感谢",
    "好的", "好滴", "嗯", "哈", "早上好", "中午好", "晚上好", "辛苦了", "再见", "拜拜",
)
_CHAT_MAX_CHARS = 16


def fast_chat(text: str) -> bool:
    """短问候/寒暄 -> True（确定性判闲聊，跳过路由与改写的小模型调用）。"""
    q = (text or "").strip().lower()
    if not q or len(q) > _CHAT_MAX_CHARS:
        return False
    if hit_data_keyword(q) or is_document_question(q):
        return False
    return any(w in q for w in _GREETING_WORDS)


# 越界请求的强特征词：路由 LLM 对「翻译合同 / 写快速排序」不稳定（实测判成
# stock_agent，还真把代码写了出来）。这些模式足够确定，先走快路径婉拒再交给 LLM。
# 只要句子里带股票/投资相关词就不拦：例如「用 Python 写个选股脚本」属于投研需求。
_OFF_TOPIC_ALLOW = (
    "股票", "选股", "行情", "财报", "证券", "投资", "a股", "量化", "回测", "基金", "债券",
    # 翻译/写作类请求若指向投研材料，仍属股票场景，不能一律婉拒
    "研报", "公告", "年报", "季报", "中报",
)
_OFF_TOPIC_PATTERNS = (
    re.compile(r"翻译"),
    re.compile(r"写[^，。]{0,12}(辞职信|周报|简历|作文|邮件|请假条|感谢信|小说|诗)"),
    re.compile(r"(python|java|golang|javascript|c\+\+|c#|sql)\s*(写|实现|编写|做|搭)"),
    re.compile(r"写[^，。]{0,12}(快速排序|冒泡排序|爬虫|脚本|代码|程序|算法|网页|网站|接口|教程)"),
    re.compile(r"(菜谱|做菜|作业)"),
)


def fast_off_topic(text: str) -> bool:
    """与股票无关的强特征请求 -> True（确定性婉拒，跳过路由 LLM）。"""
    q = (text or "").strip().lower()
    if not q:
        return False
    if any(k in q for k in _OFF_TOPIC_ALLOW):
        return False
    return any(p.search(q) for p in _OFF_TOPIC_PATTERNS)
