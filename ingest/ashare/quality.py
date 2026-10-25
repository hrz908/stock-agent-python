"""质量门禁、内容指纹与近重复检测。

两道去重各有分工：
- fingerprint 抓「同一份内容被不同源重复收录」，精确命中，零误杀；
- simhash 抓「同一份内容改了几个字」，只做候选召回，最终还要长度比与汉明距离双条件。
"""
import hashlib
import re
import unicodedata
from collections import Counter

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ALNUM_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]")
_NON_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)

_BOILERPLATE_PATTERNS = (
    "本公司及董事会全体成员保证",
    "敬请广大投资者注意投资风险",
    "特此公告",
    "特此通知",
    "证券代码：",
    "证券简称：",
    "公告编号：",
    "免责声明",
    "本报告仅供",
    "未经本公司书面许可",
)

# 各类文本的有效长度目标：达到即满分，不足按比例扣分
_TARGET_LEN = {
    "annual_report": 8000,
    "semi_annual_report": 6000,
    "quarterly_report": 3000,
    "announcement": 600,
    "research_report": 2500,
    "industry_policy": 800,
    "news": 300,
    "news_flash": 150,
}
# 低于该长度直接判定无信息量，不进入打分
_MIN_LEN = {
    "annual_report": 500,
    "semi_annual_report": 400,
    "quarterly_report": 300,
    "announcement": 60,
    "research_report": 120,
    "industry_policy": 60,
    "news": 40,
    "news_flash": 30,
}
_DEFAULT_TRUST = {"cninfo": 1.0, "eastmoney-report": 0.9, "cls": 0.85, "eastmoney": 0.8}


def cjk_ratio(text: str) -> float:
    chars = _ALNUM_RE.findall(text or "")
    if not chars:
        return 0.0
    return len(_CJK_RE.findall(text)) / len(chars)


def boilerplate_ratio(text: str) -> float:
    lines = [line.strip() for line in (text or "").split("\n") if line.strip()]
    if not lines:
        return 1.0
    hits = sum(1 for line in lines if any(p in line for p in _BOILERPLATE_PATTERNS))
    return hits / len(lines)


def score_quality(title: str, body: str, source_type: str, trust: float | None = None) -> tuple[float, list[str]]:
    """返回 (0~1 分, 拒绝原因)。分数只用于门禁和排序，不参与检索相似度。"""
    reasons: list[str] = []
    text = f"{title}\n{body}".strip()
    length = len(text)

    min_len = _MIN_LEN.get(source_type, 60)
    if length < min_len:
        return 0.0, [f"too_short({length}<{min_len})"]

    target = _TARGET_LEN.get(source_type, 800)
    len_score = min(1.0, length / target)

    cjk = cjk_ratio(text)
    if cjk < 0.25:
        reasons.append(f"low_cjk({cjk:.2f})")
    cjk_score = min(1.0, cjk / 0.5)

    bp = boilerplate_ratio(body)
    if bp > 0.5:
        reasons.append(f"boilerplate({bp:.2f})")
    bp_score = 1.0 - min(1.0, bp / 0.5)

    digits = sum(ch.isdigit() for ch in text)
    if length and digits / length > 0.4:
        reasons.append("number_heavy")
    digit_score = 1.0 - min(1.0, max(0.0, digits / max(length, 1) - 0.15) / 0.45)

    if trust is None:
        trust = _DEFAULT_TRUST.get(source_type, 0.85)

    score = trust * (0.4 * len_score + 0.3 * cjk_score + 0.2 * bp_score + 0.1 * digit_score)
    return round(score, 4), reasons


def passes_gate(score: float, reasons: list[str], min_score: float) -> bool:
    hard_fail = {"number_heavy", "boilerplate"}
    if any(r.split("(")[0] in hard_fail for r in reasons):
        return False
    return score >= min_score


def _fingerprint_text(*parts: str) -> str:
    text = "".join(parts)
    text = unicodedata.normalize("NFKC", text).lower()
    return _NON_WORD_RE.sub("", text)


def content_fingerprint(title: str, codes: list[str], pub_date: str, body: str) -> str:
    """跨源去重指纹：不含来源，含代码与日期。

    带日期是必须的——同一家公司每月都发《回购进展公告》，标题与正文模板几乎一致，
    不带日期会把后续月份的公告全部误判成重复。
    """
    payload = _fingerprint_text(title, "|", ",".join(codes), "|", pub_date, "|", body)
    return hashlib.sha256(payload.encode("utf-8", "ignore")).hexdigest()


def source_hash(source_id: str, title: str, pub_time: str) -> str:
    return hashlib.sha1(f"{source_id}|{title}|{pub_time}".encode("utf-8", "ignore")).hexdigest()


def _simhash_tokens(text: str) -> Counter:
    compact = _NON_WORD_RE.sub("", unicodedata.normalize("NFKC", text or "").lower())
    tokens: list[str] = []
    if len(compact) >= 3:
        tokens.extend(compact[i:i + 3] for i in range(len(compact) - 2))
    else:
        tokens.append(compact)
    tokens.extend(re.findall(r"[a-z0-9]{2,}", (text or "").lower()))
    # 只取高频前 400 个：长文档全量算 64 位开销过大，前 400 已足够区分内容
    return Counter(tokens).most_common(400)


def simhash64(text: str) -> int:
    weights = [0] * 64
    for token, weight in _simhash_tokens(text):
        h = int.from_bytes(hashlib.md5(token.encode("utf-8", "ignore")).digest()[:8], "big")
        for bit in range(64):
            weights[bit] += weight if (h >> bit) & 1 else -weight
    value = 0
    for bit in range(64):
        if weights[bit] > 0:
            value |= 1 << bit
    return value


def simhash_bands(value: int) -> list[int]:
    """4 段 16bit。汉明≤3 时必有一段完全相同，用 GIN 索引即可召回候选。"""
    return [(value >> shift) & 0xFFFF for shift in (0, 16, 32, 48)]


def to_signed64(value: int) -> int:
    """BIGINT 存不下 2^63~2^64-1，落库前转成有符号。"""
    return value - (1 << 64) if value >= (1 << 63) else value


def from_signed64(value: int) -> int:
    return value & 0xFFFFFFFFFFFFFFFF


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & 0xFFFFFFFFFFFFFFFF).bit_count()
