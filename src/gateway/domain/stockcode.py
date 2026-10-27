"""股票代码归一化与展示格式。

与 Go 版 internal/domain/stockcode 一致，交易所前缀表必须与小程序前端
EXCHANGES 保持同步：前端只提交 6 位数字时，后端按同一套规则反推市场。
"""

from __future__ import annotations

import re

MARKET_SH = "SH"
MARKET_SZ = "SZ"
MARKET_BJ = "BJ"

_CODE_RE = re.compile(r"^(SH|SZ|BJ)\d{6}$")
_DIGIT_RE = re.compile(r"^\d{6}$")

# 顺序敏感：先匹配到的前缀优先
_MARKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (MARKET_SH, ("600", "601", "603", "605", "688", "689")),
    (MARKET_SZ, ("000", "001", "002", "003", "300", "301", "302")),
    (MARKET_BJ, ("43", "83", "87", "88", "920")),
)


def market_of(digits: str) -> str:
    """由 6 位代码推断市场，推断不出返回空串。"""
    for prefix, codes in _MARKETS:
        for head in codes:
            if digits.startswith(head):
                return prefix
    return ""


def normalize_code(raw: str) -> tuple[str, bool]:
    """统一成「市场前缀 + 6 位数字」。

    兼容小程序带空格的展示格式（"SH 600519"），也允许只传 6 位数字。
    """
    text = "".join((raw or "").split()).upper()
    if _CODE_RE.match(text):
        return text, True
    if not _DIGIT_RE.match(text):
        return "", False
    market = market_of(text)
    if not market:
        return "", False
    return market + text, True


def display_code(code: str) -> str:
    """还原成小程序列表里的展示格式："SH 600519"。"""
    if len(code) < 3:
        return code
    return f"{code[:2]} {code[2:]}"


def digits_of(code: str) -> str:
    """取 6 位裸码（内部候选带市场前缀，对外只给裸码）。"""
    text = (code or "").strip()
    if len(text) >= 8 and text[:2].isalpha():
        return text[2:]
    return text
