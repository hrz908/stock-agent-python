"""去重保序。

Go 版 util.Unique 同样保序：失败集合靠「去重后请求列表 − 成功集合」求差集，
顺序被打乱会让错误提示与用户操作顺序对不上。
"""

from __future__ import annotations

from collections.abc import Iterable


def unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def chunk(items: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        size = 1
    return [items[i : i + size] for i in range(0, len(items), size)]
