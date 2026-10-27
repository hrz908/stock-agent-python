"""腾讯 smartbox 搜索联想。

与行情接口完全不同，三个坑已在真实样本上固化：
1. 按 ^ 分条、~ 分字段，字段以 **market 打头**而不是 code；
2. 中文是 \\uXXXX 转义文本，绝不能套 GBK 解码器；
3. 会混入港股 / 基金，必须按 market + type 过滤，只留 A 股。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from server.quote.source import MAX_BODY_BYTES, SMARTBOX_URL

logger = logging.getLogger("stocka.server.quote.search")

_UNICODE_ESCAPE = re.compile(r"\\u[0-9a-fA-F]{4}")
_VAR_ASSIGN = re.compile(r'v_[a-z0-9]+="([^"]*)"')


@dataclass(slots=True)
class SearchResult:
    code: str
    market: str
    name: str


def _decode_unicode_escape(text: str) -> str:
    """把 \\uXXXX 还原成真实字符。

    代理对（\\ud83d\\ude00）必须先合并高低位，单个解出来是无效码元会变成替换符。
    """
    matches = list(_UNICODE_ESCAPE.finditer(text))
    if not matches:
        return text

    out: list[str] = []
    pos = 0
    index = 0
    while index < len(matches):
        match = matches[index]
        out.append(text[pos : match.start()])
        code_unit = int(match.group(0)[2:], 16)

        if 0xD800 <= code_unit <= 0xDBFF and index + 1 < len(matches):
            next_match = matches[index + 1]
            if next_match.start() == match.end():
                low = int(next_match.group(0)[2:], 16)
                if 0xDC00 <= low <= 0xDFFF:
                    combined = 0x10000 + ((code_unit - 0xD800) << 10) + (low - 0xDC00)
                    out.append(chr(combined))
                    index += 2
                    pos = next_match.end()
                    continue

        if 0xD800 <= code_unit <= 0xDFFF:
            # 孤立代理元：丢弃，避免写出非法字符
            pos = match.end()
            index += 1
            continue

        out.append(chr(code_unit))
        pos = match.end()
        index += 1

    out.append(text[pos:])
    return "".join(out)


def parse_smartbox(body: str, limit: int) -> list[SearchResult]:
    out: list[SearchResult] = []
    matched = _VAR_ASSIGN.search(body)
    if matched is None:
        return out

    for line in matched.group(1).split("^"):
        if len(out) >= limit:
            break
        fields = line.split("~")
        if len(fields) < 3:
            continue

        market = fields[0].strip().lower()
        code = fields[1].strip()
        name = _decode_unicode_escape(fields[2].strip())

        # 只要 A 股三个市场，过滤掉港股 / 基金 / 债券
        if market not in ("sh", "sz", "bj"):
            continue
        # A 股代码固定 6 位数字
        if len(code) != 6 or not code.isdigit():
            continue
        # type 存在时必须是股票（GP / GP-A），排除基金 KJ-* 等
        if len(fields) >= 5:
            kind = fields[4].strip().upper()
            if kind and not kind.startswith("GP"):
                continue

        out.append(SearchResult(code=code, market=market.upper(), name=name or code))
    return out


async def search(query: str, limit: int) -> list[SearchResult]:
    """模糊搜索：支持代码前缀、汉字名、拼音缩写（maotai / gzmt）。

    失败返回空列表——搜索是体验加分项，不该让主流程报错。
    """
    from server.deps import runtime

    keyword = (query or "").strip()
    if not keyword or limit <= 0:
        return []

    client = runtime().service("market_source").search_client
    try:
        response = await client.get(SMARTBOX_URL, params={"q": keyword, "t": "all"})
        if response.status_code != 200:
            logger.info("search api http %d: q=%s", response.status_code, keyword)
            return []
        # smartbox 返回纯 ASCII 文本，不能套 GBK 解码
        body = response.content[:MAX_BODY_BYTES].decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        logger.info("search request failed: q=%s err=%s", keyword, exc)
        return []

    parsed = parse_smartbox(body, limit)
    if not parsed:
        logger.info(
            "search parsed 0 items: q=%s status=%d raw=%.200s",
            keyword,
            response.status_code,
            body,
        )
    return parsed
