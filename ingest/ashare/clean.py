"""文本清洗：HTML 去噪、PDF 版面残留清理、财报章节剔除、政策与公告类型识别。

清洗顺序有意固定：先去版面噪声，再去模板章节，最后做数值行过滤。
反过来会把「合并资产负债表」这行表格标题先删掉，后面的整段报表就失去了切除锚点。
"""
import re
import unicodedata
from collections import Counter

_CJK = r"\u4e00-\u9fff"
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE_RE = re.compile(r"[ \t]+")
_CJK_SPACE_RE = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")
_PAGE_NO_RE = re.compile(r"^\s*[-—–]?\s*(?:第\s*)?\d{1,4}\s*(?:页)?\s*[-—–]?\s*$")
_PAGE_OF_RE = re.compile(r"^\s*第\s*\d+\s*页\s*(?:共\s*\d+\s*页)?\s*(?:/\s*\d+\s*)?$")
_WATERMARK_RE = re.compile(
    r"^(?:巨潮资讯网|东方财富网|新浪财经|同花顺|http[s]?://\S+|www\.\S+)$"
    r"|敬请参阅最后一页|请务必阅读正文之后的?免责声明|本页无正文"
)


def strip_html(html: str) -> str:
    """HTML 片段转纯文本；bs4 缺失时退化为正则去标签。"""
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        return re.sub(r"<[^>]+>", "\n", text)

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    return soup.get_text("\n")


def normalize(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\u3000", " ").replace("\u200b", "").replace("\ufeff", "")
    text = _CTRL_RE.sub("", text)

    lines = []
    for line in text.split("\n"):
        line = _SPACE_RE.sub(" ", line).strip()
        # PDF 抽字常在汉字之间插空格，按行去掉；跨行不动，避免把段落压成一行
        line = _CJK_SPACE_RE.sub("", line)
        lines.append(line)
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _remove_repeated_lines(text: str, min_repeat: int = 5, max_len: int = 40) -> str:
    """重复 ≥5 次且短的行视为页眉页脚水印，整行删掉。"""
    lines = text.split("\n")
    counter = Counter(line.strip() for line in lines if 0 < len(line.strip()) <= max_len)
    repeated = {line for line, count in counter.items() if count >= min_repeat}
    if not repeated:
        return text
    return "\n".join(line for line in lines if line.strip() not in repeated)


def clean_pdf_noise(text: str) -> str:
    text = _remove_repeated_lines(text)
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            out.append("")
            continue
        if _PAGE_NO_RE.match(s) or _PAGE_OF_RE.match(s) or _WATERMARK_RE.search(s):
            continue
        out.append(line)
    text = "\n".join(out)
    # 英文断词换行：informa-\ntion -> information
    return re.sub(r"-\n(?=[a-z])", "", text)


# ---------------------------------------------------------------------------
# 财报章节剔除
# ---------------------------------------------------------------------------
_STATEMENT_NAMES = ("资产负债表", "利润表", "现金流量表", "所有者权益变动表")
_STMT_LEAD_RE = re.compile(r"^[\s#\d０-９一二三四五六七八九十、.．()（）附注续：:]+")
_SECTION_HEAD_RE = re.compile(
    r"^(#{1,6}\s|第[一二三四五六七八九十百]+[节章]|[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\d+[、.])"
)
_PROSE_RE = re.compile(r".{40,}[。；：:]$")


def _is_statement_head(s: str) -> bool:
    if not s or len(s) > 30:
        return False
    body = _STMT_LEAD_RE.sub("", s).replace("（续）", "").replace("(续)", "").strip(" ：:")
    return any(body == name or body == f"{prefix}{name}"
               for name in _STATEMENT_NAMES for prefix in ("合并", "母公司", "公司"))


def _is_section_head(s: str) -> bool:
    return len(s) <= 40 and bool(_SECTION_HEAD_RE.match(s))


def drop_financial_statements(text: str, max_skip_lines: int = 600) -> str:
    """丢掉三大报表正文，保留「财务报表附注」等文字章节。

    报表是一整片数字，向量化后既占存储又污染检索；而附注里的会计政策、
    关联交易、或有事项是真正的文本信息，必须保留。
    """
    out: list[str] = []
    skipping = False
    skipped = 0
    for line in text.split("\n"):
        s = line.strip()
        if not skipping:
            if _is_statement_head(s):
                skipping = True
                skipped = 0
                continue
            out.append(line)
            continue

        skipped += 1
        if skipped > max_skip_lines:
            skipping = False
            out.append(line)
            continue
        # 命中下一个章节标题或恢复叙述性长句，认为报表结束
        if (_is_section_head(s) and not _is_statement_head(s)) or (len(s) >= 40 and _PROSE_RE.match(s)):
            skipping = False
            out.append(line)
    return "\n".join(out)


def _is_numeric_line(line: str) -> bool:
    s = line.strip()
    if len(s) < 8:
        return False
    if s.count("|") >= 2:
        cells = [c for c in s.strip("|").split("|") if c.strip()]
        if len(cells) < 3:
            return False
        numeric = sum(ch.isdigit() or ch in ".,%％-—–()（）" for ch in s)
        return numeric / len(s) > 0.35
    digits = sum(ch.isdigit() for ch in s)
    return digits / len(s) > 0.5


def filter_numeric_lines(text: str) -> str:
    """剔除纯数字/数字占多数的行；文字型表格（分产品收入构成等）保留。"""
    return "\n".join(line for line in text.split("\n") if not _is_numeric_line(line))


# ---------------------------------------------------------------------------
# 模板尾巴
# ---------------------------------------------------------------------------
_TAIL_MARKERS = (
    "免责声明", "重要声明", "分析师承诺", "投资评级说明", "评级说明",
    "信息披露声明", "特别声明", "法律声明", "风险揭示",
)


def cut_boilerplate_tail(text: str) -> str:
    """只切后半段出现的免责声明；研报的评级说明动辄占三成篇幅，向量化毫无价值。"""
    cut = len(text)
    half = len(text) * 0.5
    for marker in _TAIL_MARKERS:
        idx = text.find(marker)
        while idx != -1:
            if idx > half:
                cut = min(cut, idx)
                break
            idx = text.find(marker, idx + 1)
    return text[:cut].rstrip()


_SIGN_RE = re.compile(r"^(特此公告|特此通知)[。.]?$|^\S{2,30}(董事会|监事会|股东大会)$")
_DATE_LINE_RE = re.compile(r"^\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日$")


def strip_tail_signature(text: str) -> str:
    lines = text.rstrip().split("\n")
    removed = 0
    while lines and removed < 8:
        s = lines[-1].strip()
        if not s or _SIGN_RE.match(s) or _DATE_LINE_RE.match(s):
            lines.pop()
            removed += 1
            continue
        break
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------
_PERIODIC_NEGATIVE = (
    "说明会", "预约", "审计", "编制", "更正", "问询", "意见", "披露",
    "解读", "点评", "电话会议", "工作进展", "进展公告",
)


def periodic_type(title: str) -> str:
    """按标题识别定期报告。宁可漏判（降级为公告）也不误判，误判会整篇丢报表章节。"""
    t = re.sub(r"[\s（）()【】\[\]“”\"]", "", title or "")
    if "半年度报告" in t:
        return "semi_annual_report"
    if "季度报告" in t and any(k in t for k in ("第一季度", "一季度", "第三季度", "三季度")):
        return "quarterly_report"
    if "年度报告" in t and not any(k in t for k in _PERIODIC_NEGATIVE):
        return "annual_report"
    return ""


_EVENT_RULES = (
    ("回购", "股份回购"),
    ("增持", "股东增持"),
    ("减持", "股东减持"),
    ("业绩预告", "业绩预告"),
    ("业绩快报", "业绩快报"),
    ("重大合同", "重大合同"),
    ("中标", "中标"),
    ("重组", "资产重组"),
    ("收购", "收购"),
    ("定增", "定向增发"),
    ("向特定对象发行", "定向增发"),
    ("可转债", "可转债"),
    ("分红", "权益分派"),
    ("利润分配", "权益分派"),
    ("股权激励", "股权激励"),
    ("关联交易", "关联交易"),
    ("担保", "对外担保"),
    ("诉讼", "诉讼仲裁"),
    ("处罚", "监管处罚"),
    ("问询", "监管问询"),
    ("停牌", "停复牌"),
    ("退市", "退市风险"),
    ("风险提示", "风险提示"),
    ("人事", "人事变动"),
    ("辞职", "人事变动"),
    ("聘任", "人事变动"),
)


def announcement_event_type(title: str) -> str:
    for keyword, label in _EVENT_RULES:
        if keyword in (title or ""):
            return label
    return ""


_IMPORTANT_ANNOUNCEMENT = (
    "回购", "增持", "减持", "业绩预告", "业绩快报", "重大合同", "中标",
    "重组", "收购", "定增", "向特定对象发行", "可转债", "分红", "利润分配",
    "股权激励", "关联交易", "担保", "诉讼", "处罚", "问询", "停产", "退市",
    "风险提示", "实际控制人", "控股股东",
    # 更正后的定期报告：标题带「更正」不会命中 periodic_type，正文仍必须抓
    "年度报告", "半年度报告", "季度报告",
)


def is_important_announcement(title: str) -> bool:
    return any(k in (title or "") for k in _IMPORTANT_ANNOUNCEMENT)


_POLICY_ORGS = (
    "国务院", "国家发展改革委", "国家发改委", "发改委", "工业和信息化部", "工信部",
    "财政部", "中国人民银行", "中国证监会", "证监会", "商务部", "国家能源局",
    "生态环境部", "国家卫生健康委", "国家医保局", "市场监管总局", "科技部",
    "住房城乡建设部", "交通运输部", "农业农村部", "国家数据局", "国家金融监督管理总局",
)
_POLICY_ACTIONS = (
    "印发", "发布", "出台", "实施", "征求意见", "通知", "指导意见",
    "若干措施", "管理办法", "规划", "方案", "试点", "批复",
)


def policy_org(title: str, body: str = "") -> str:
    """返回发文机构名；非政策类返回空串。

    先在标题里找「机构+动作」，找不到才看正文前 1000 字——正文里机构名往往是
    转述（「据证监会消息」），标题命中更可信。
    """
    title = title or ""
    for org in _POLICY_ORGS:
        if org in title and any(a in title for a in _POLICY_ACTIONS):
            return org
    head = (body or "")[:1000]
    for org in _POLICY_ORGS:
        if org in head and any(a in head for a in _POLICY_ACTIONS):
            return org
    return ""


def guess_title(text: str, limit: int = 40) -> str:
    """快讯标题常为空，用首句兜底；截断时避免切在半个括号里。"""
    first = re.split(r"[。！？!?\n]", (text or "").strip(), maxsplit=1)[0].strip()
    if len(first) <= limit:
        return first
    return first[:limit].rstrip("（(，,、;；") + "…"
