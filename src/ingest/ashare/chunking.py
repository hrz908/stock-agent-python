"""中文感知分块：标题/章节分段，段落装满，长段落再按句子切。

与 ingest/chunker.py 的区别：按「字符」而不是 cl100k token 计数。中文财经文本
在 cl100k 下几乎一字一 token，先用 token 计数只是绕一圈算字符，还会把「2026 年
4 月 30 日」这类数字串切碎。这里直接以字符预算控制，边界永远落在句号/换行上。
"""
import re
from dataclasses import dataclass

from ingest.ashare.types import SOURCE_TYPE_LABELS

_HEAD_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_CN_SECTION_RE = re.compile(r"^第[一二三四五六七八九十百]+[节章]\s*\S")
_SENT_RE = re.compile(r"(?<=[。！？!?；;])\s*")

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_ENC.encode(text))

except ImportError:  # tiktoken 缺失时退化为字符数，仅用于统计
    def count_tokens(text: str) -> int:
        return len(text or "")


@dataclass(slots=True)
class TextChunk:
    seq: int
    content: str
    section: str
    token_count: int


def _heading(line: str) -> tuple[int, str] | None:
    stripped = line.strip()
    m = _HEAD_RE.match(stripped)
    if m:
        return len(m.group(1)), m.group(2).strip()
    if len(stripped) <= 40 and _CN_SECTION_RE.match(stripped):
        return 2, stripped
    return None


def split_sections(text: str) -> list[tuple[str, str]]:
    """按 Markdown 标题或「第X节」切段，返回 [(标题路径, 正文)]。"""
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []

    def flush():
        if buffer:
            path = " > ".join(title for _, title in stack)
            sections.append((path, "\n".join(buffer)))
            buffer.clear()

    for line in (text or "").split("\n"):
        head = _heading(line)
        if head is None:
            buffer.append(line)
            continue
        flush()
        level, title = head
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        buffer.append(line)
    flush()
    return [(path, body) for path, body in sections if body.strip()]


def _sentences(text: str) -> list[str]:
    return [s for s in _SENT_RE.split(text) if s and s.strip()]


def _split_long(text: str, max_chars: int, overlap: int) -> list[str]:
    """超长段落按句子装满；单句仍超长才硬切，并在硬切处保留 overlap。"""
    chunks: list[str] = []
    buffer = ""
    for sentence in _sentences(text):
        if len(sentence) > max_chars:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            step = max(1, max_chars - overlap)
            for i in range(0, len(sentence), step):
                chunks.append(sentence[i:i + max_chars])
            continue
        if not buffer:
            buffer = sentence
        elif len(buffer) + len(sentence) <= max_chars:
            buffer += sentence
        else:
            chunks.append(buffer)
            # 只承接上一块最后一句，避免从半个词开始
            tail = _sentences(buffer)[-1] if overlap else ""
            buffer = tail + sentence if tail and len(tail) + len(sentence) <= max_chars else sentence
    if buffer:
        chunks.append(buffer)
    return chunks


def _pack_section(body: str, max_chars: int, overlap: int) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    if not paragraphs:
        return []
    chunks: list[str] = []
    buffer = ""
    for para in paragraphs:
        if len(para) > max_chars:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.extend(_split_long(para, max_chars, overlap))
            continue
        if not buffer:
            buffer = para
        elif len(buffer) + 1 + len(para) <= max_chars:
            buffer += "\n" + para
        else:
            chunks.append(buffer)
            buffer = para
    if buffer:
        chunks.append(buffer)
    return chunks


def _context_header(ctx: dict, section: str) -> str:
    """给每块加一行实体上下文。

    公告正文大量出现「本次回购价格不超过…」这类无主语句子，脱离标题后既检不到
    也答不准；而标题里的公司名/代码是最稳定的过滤与语义锚点。
    """
    parts: list[str] = []
    names = ctx.get("stock_names") or []
    codes = ctx.get("stock_codes") or []
    if names:
        parts.append(f"{names[0]}({codes[0]})" if codes else names[0])
    elif codes:
        parts.append(codes[0])
    label = SOURCE_TYPE_LABELS.get(ctx.get("source_type", ""), "")
    if label:
        parts.append(label)
    if ctx.get("pub_date"):
        parts.append(ctx["pub_date"])
    if section:
        # Markdown 标题的 # 号不进上下文，否则每块都以「# 」开头，污染检索与展示
        cleaned = re.sub(r"^[#\s]+", "", section).replace(" > ", "/")
        parts.append(cleaned[:40])
    return f"【{' · '.join(parts)}】" if parts else ""


def chunk_document(
    body: str,
    ctx: dict,
    max_chars: int = 450,
    min_chars: int = 60,
    overlap: int = 80,
) -> list[TextChunk]:
    """把正文切成带上下文的块；过短块并入前块，避免产生只有「特此公告。」的向量。"""
    header = _context_header(ctx, "")
    budget = max(120, max_chars - len(header) - 1)

    pieces: list[tuple[str, str]] = []
    for section, section_body in split_sections(body):
        for piece in _pack_section(section_body, budget, overlap):
            pieces.append((section, piece))
    if not pieces and body.strip():
        pieces = [("", body.strip())]

    merged: list[tuple[str, str]] = []
    for section, piece in pieces:
        if merged and len(piece) < min_chars:
            prev_section, prev_piece = merged[-1]
            if len(prev_piece) + 1 + len(piece) <= max_chars + 60:
                merged[-1] = (prev_section, prev_piece + "\n" + piece)
                continue
        # 整篇就一小段（短快讯）必须保留；只有前后还有长块时，碎片才值得丢
        if len(piece) < min_chars and not merged and len(pieces) > 1:
            continue
        merged.append((section, piece))

    chunks: list[TextChunk] = []
    for section, piece in merged:
        head = _context_header(ctx, section) or header
        content = f"{head}\n{piece}".strip() if head else piece
        chunks.append(TextChunk(seq=len(chunks), content=content, section=section, token_count=count_tokens(content)))
    return chunks
