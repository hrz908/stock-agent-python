"""中文检索词元：UTF-8 中文按 bigram 切分，ASCII 代码/英文词原样保留。

与 ``agent/rag/zh_tokenize.py`` 保持一致（两个镜像的 build context 独立，不能共享包）。
入库时把 ``document_text`` 写进 ``chunks.search_text``，检索时用 ``query_tsquery``。
"""
import re

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_RE = re.compile(r"[A-Za-z0-9]+")
_STOP_UNIGRAMS = frozenset("的了和与是在有吗呢啊这那我你他它们就也都")


def _all_tokens(text: str) -> list[str]:
    text = text or ""
    out: list[str] = []
    for run in _CJK_RE.findall(text):
        if len(run) == 1:
            if run not in _STOP_UNIGRAMS:
                out.append(run)
            continue
        out.extend(run[i:i + 2] for i in range(len(run) - 1))
    out.extend(m.group(0).lower() for m in _ASCII_RE.finditer(text))
    return out


def document_text(text: str) -> str:
    """文档入库：保留重复 bigram，供 ts_rank_cd 计算词频。"""
    return " ".join(_all_tokens(text))


def query_tsquery(text: str, limit: int = 32) -> str:
    """查询侧：去重并限制词元数，拼成 OR 形式的 tsquery。"""
    toks = list(dict.fromkeys(_all_tokens(text)))
    if not toks:
        return "__empty__"
    return " | ".join(toks[:limit])
