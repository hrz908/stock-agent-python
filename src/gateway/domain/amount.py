"""金额/比例格式化。

对外一律输出字符串，且必须用 Decimal 定点计算——中间走一次 float
就会出现 1705.2999999999999 这类值，前端口径也会跟着漂。

与 Go 版 util/amount.go 逐条对齐：
- Money：2 位小数；
- Thousands：千分位 + 2 位，保留负号；
- WithSign：带正负号，并把 "-0.00" 归一成 "0.00"（否则前端显示负零）。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_CENT = Decimal("0.01")
_HUNDRED_MILLION = Decimal(100000000)


def money(value: Decimal) -> str:
    """金额统一 2 位小数。"""
    return str(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def thousands(value: Decimal) -> str:
    """千分位 + 2 位小数：1705.30 → 1,705.30。"""
    text = str(value.quantize(_CENT, rounding=ROUND_HALF_UP))
    negative = text.startswith("-")
    if negative:
        text = text[1:]

    int_part, _, frac = text.partition(".")
    grouped = []
    for index, ch in enumerate(int_part):
        if index > 0 and (len(int_part) - index) % 3 == 0:
            grouped.append(",")
        grouped.append(ch)

    out = "".join(grouped) + ("." + frac if frac else "")
    return "-" + out if negative else out


def with_sign(value: Decimal) -> str:
    """带正负号（+1.23 / -0.86 / 0.00）。

    -0.001 经 2 位定长会得到 "-0.00"，先归一成 "0.00"，否则前端显示负零。
    """
    text = str(value.quantize(_CENT, rounding=ROUND_HALF_UP))
    if text == "-0.00":
        text = "0.00"
    if value > 0:
        return "+" + text
    return text


def yi_to_yuan(value: Decimal) -> Decimal:
    """亿元 → 元（腾讯总市值/流通市值口径）。"""
    return value * _HUNDRED_MILLION
