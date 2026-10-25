"""文本分块器：按 Markdown 标题层级切分，超长块滑动窗口再切。"""
import logging
import re

import tiktoken

logger = logging.getLogger(__name__)

# 全局编码器（cl100k_base 近似，实际 embedding 模型可能不同）
_enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """计算文本的 token 数"""
    return len(_enc.encode(text))


def _sliding_window(text: str, chunk_size: int, overlap: int) -> list[str]:
    """滑动窗口切分：按 token 计数，带 overlap"""
    tokens = _enc.encode(text)
    if len(tokens) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(tokens):
        end = min(start + chunk_size, len(tokens))
        chunk_text = _enc.decode(tokens[start:end])
        chunks.append(chunk_text)
        if end >= len(tokens):
            break
        start += chunk_size - overlap
    return chunks


def _heading_level(line: str) -> int | None:
    """解析 Markdown 标题级别，非标题返回 None"""
    m = re.match(r'^(#{1,6})\s+(.+)$', line)
    if m:
        return len(m.group(1))
    return None


def _heading_text(line: str) -> str:
    """提取标题文本"""
    m = re.match(r'^#{1,6}\s+(.+)$', line)
    return m.group(1).strip() if m else ""


def chunk_markdown(content: str, chunk_size: int = 512, overlap: int = 64) -> list[dict]:
    """对 Markdown 内容分块：先按标题层级切 section，超长 section 再按段落切，
    段落仍超长则用滑动窗口（chunk_size / overlap）；最后给每个 chunk 头部注入标题路径。

    返回 [{"seq", "content", "token_count", "metadata": {"heading_path"}}]。
    """
    lines = content.split("\n")

    # 按标题层级切分为 sections
    sections: list[dict] = []  # {"heading_path": str, "lines": list[str]}
    heading_stack: list[str] = []  # 当前标题路径栈 [(level, text), ...]
    heading_levels: list[int] = []
    current_lines: list[str] = []

    for line in lines:
        level = _heading_level(line)
        if level is not None:
            if current_lines:
                path = " > ".join(
                    f"{'#' * lv} {t}" for lv, t in zip(heading_levels, heading_stack, strict=True)
                )
                sections.append({"heading_path": path, "lines": current_lines})
                current_lines = []

            # 更新标题栈：弹出同级或更低级的标题
            while heading_stack and heading_levels and heading_levels[-1] >= level:
                heading_stack.pop()
                heading_levels.pop()
            heading_stack.append(_heading_text(line))
            heading_levels.append(level)

            # 标题行本身也作为 section 内容的一部分
            current_lines.append(line)
        else:
            current_lines.append(line)

    # 最后一个 section
    if current_lines:
        path = " > ".join(
            f"{'#' * lv} {t}" for lv, t in zip(heading_levels, heading_stack, strict=True)
        )
        sections.append({"heading_path": path, "lines": current_lines})

    # 对每个 section 做 token 级别的切分
    result: list[dict] = []
    seq = 0

    for section in sections:
        section_text = "\n".join(section["lines"]).strip()
        if not section_text:
            continue

        heading_path = section["heading_path"] or "(root)"
        token_count = count_tokens(section_text)

        if token_count <= chunk_size:
            # 整块作为一个 chunk
            result.append({
                "seq": seq,
                "content": section_text,
                "token_count": token_count,
                "metadata": {"heading_path": heading_path},
            })
            seq += 1
        else:
            # 尝试按段落（双换行）进一步切分
            paragraphs = re.split(r'\n{2,}', section_text)
            buffer = ""
            buffer_tokens = 0

            for para in paragraphs:
                para = para.strip()
                if not para:
                    continue
                para_tokens = count_tokens(para)

                if buffer_tokens + para_tokens <= chunk_size:
                    buffer = (buffer + "\n\n" + para) if buffer else para
                    buffer_tokens += para_tokens
                else:
                    # 先保存 buffer
                    if buffer:
                        result.append({
                            "seq": seq,
                            "content": buffer,
                            "token_count": buffer_tokens,
                            "metadata": {"heading_path": heading_path},
                        })
                        seq += 1

                    # 如果单个段落就超长，滑动窗口切
                    if para_tokens > chunk_size:
                        windows = _sliding_window(para, chunk_size, overlap)
                        for w in windows:
                            w_tokens = count_tokens(w)
                            result.append({
                                "seq": seq,
                                "content": w,
                                "token_count": w_tokens,
                                "metadata": {"heading_path": heading_path},
                            })
                            seq += 1
                        buffer = ""
                        buffer_tokens = 0
                    else:
                        buffer = para
                        buffer_tokens = para_tokens

            # 保存剩余 buffer
            if buffer:
                result.append({
                    "seq": seq,
                    "content": buffer,
                    "token_count": buffer_tokens,
                    "metadata": {"heading_path": heading_path},
                })
                seq += 1

    # 注入标题路径到 content 头部
    for chunk in result:
        hp = chunk["metadata"]["heading_path"]
        if hp and hp != "(root)":
            chunk["content"] = f"[{hp}]\n{chunk['content']}"
            chunk["token_count"] = count_tokens(chunk["content"])

    logger.info("分块完成: %d chunks", len(result))
    return result
