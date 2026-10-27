"""流式输出净化：剥掉模型漏出的提示词标签。

背景（2026-10-26 线上实测）：闲聊子图只发 SystemMessage，问句只嵌在 system
提示的 <user_query> 标签里，模型会把整份提示词当成「待补全的文档」续写，
直接向用户输出：

    <user_query>襄阳轴承（000678）现在什么价？</user_query>
    <system_context>…</system_context>
    <assistant_response>抱歉，我这边没有实时行情接口…</assistant_response>

用户看到的就是「AI 把自己的问题当成了回答」。根因在提示词结构（见
subgraphs/chat.py 与 prompts/chat.xml），但提示词约束拦不住所有模型版本，
所以这里再做一层确定性剥离：token 流上遇到已知标签就丢掉标签本身，
跳过 <user_query>/<system_context> 这类「不该给用户看」的块，保留
<assistant_response>/<response> 里的正文。

只做流式增量，不改变已生成内容：正常回答（不含标签）零改动、零延迟——
缓冲为空且本段没有 '<' 时整段直接透传。
"""
import re

# 整块丢弃：这些块是提示词的一部分，泄漏出来只会让用户困惑
_SKIP_BLOCKS = frozenset({
    "user_query", "system_context", "system_prompt", "analysis",
    "thinking", "reasoning", "scratchpad",
})

# 只脱壳、保留正文：模型习惯把答案包在这些标签里
_UNWRAP_TAGS = frozenset({
    "assistant_response", "response", "answer", "output", "reply", "final",
})

_TAGS = _SKIP_BLOCKS | _UNWRAP_TAGS

# 标签里允许出现的字符很少；超过这个长度还没闭合，就当普通文本（如「a < b」）
_MAX_TAG_CHARS = 48

# 段落去重的最小长度：短句（「走势：」「- 结论」这类标签）重复出现是正常的，
# 只有成段的完整内容重复才视为冗余
_DUP_PARAGRAPH_MIN_CHARS = 30


def dedupe_answer_text(text: str) -> str:
    """丢弃正文里完全重复的段落，只保留第一次出现的那段。

    模型偶发把同一段结论写两遍：同轮工具数据重复（同一工具被调多次）时尤其常见。
    这里只做确定性去重——按空行切段、抹掉空白后完全相等且长度达标的段落才丢，
    不做语义近似判断，也不碰代码块（含 ``` 时整段原样返回）。
    返回值与入参在「没有重复」时逐字相同，调用方可以安全地无条件使用。
    """
    if not text or "```" in text:
        return text
    parts = re.split(r"\n{2,}", text)
    seen: set[str] = set()
    kept: list[str] = []
    for part in parts:
        key = re.sub(r"\s+", "", part)
        if len(key) >= _DUP_PARAGRAPH_MIN_CHARS:
            if key in seen:
                continue
            seen.add(key)
        kept.append(part)
    if len(kept) == len(parts):
        return text
    return "\n\n".join(kept)


class StreamCleaner:
    """把 token 增量喂进来，返回该下发给前端的文本。"""

    def __init__(self) -> None:
        self._buf = ""
        # 正在跳过的块名：非空时丢弃内容直到对应的闭合标签
        self._skip_name = ""
        # 已匹配到 "</name"，等 '>' 收尾（闭合标签可能被拆帧）
        self._closing = False
        # 还没输出过正文：开头因剥标签留下的空白一并裁掉
        self._started = False

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        # 快路径：无残留、无跳过态、本段无 '<'，原样返回（绝大多数 token 走这里）
        if not self._buf and not self._skip_name and "<" not in chunk:
            return self._emit(chunk)
        self._buf += chunk
        return self._emit(self._drain())

    def flush(self) -> str:
        """流结束：把残留缓冲按「像不像半个标签」决定去留。"""
        if self._skip_name:
            # 块没闭合就结束了：这段内容本来就不该给用户看
            self._buf = ""
            self._skip_name = ""
            self._closing = False
            return ""
        tail = self._buf
        self._buf = ""
        if not tail:
            return ""
        if tail.startswith("<"):
            # 只有「像半个已知标签」才丢；「若 PE < 10」这类正文必须原样保留
            name = _tag_name(tail[1:].split(">")[0].strip())
            if name and any(t.startswith(name) for t in _TAGS):
                return ""
        return self._emit(tail)

    def _emit(self, text: str) -> str:
        if not text:
            return ""
        if not self._started:
            text = text.lstrip()
            if not text:
                return ""
            self._started = True
        return text

    def _drain(self) -> str:
        out: list[str] = []
        while self._buf:
            if self._skip_name:
                if self._closing:
                    gt = self._buf.find(">")
                    if gt < 0:
                        # 闭合标签还没收尾：丢掉这段（只会是标签自身的字符）
                        self._buf = ""
                        break
                    self._buf = self._buf[gt + 1:]
                    self._skip_name = ""
                    self._closing = False
                    continue
                close = "</" + self._skip_name
                i = self._buf.lower().find(close)
                if i < 0:
                    # 闭合标签可能被拆到下一帧：只留尾部用于匹配，其余整段丢弃
                    keep = len(close) + 1
                    if len(self._buf) > keep:
                        self._buf = self._buf[-keep:]
                    break
                self._buf = self._buf[i + len(close):]
                self._closing = True
                continue

            i = self._buf.find("<")
            if i < 0:
                out.append(self._buf)
                self._buf = ""
                break
            if i > 0:
                out.append(self._buf[:i])
                self._buf = self._buf[i:]

            gt = self._buf.find(">")
            if gt < 0:
                # 标签跨帧：先攒着；攒太长说明不是标签，按普通文本放行
                if len(self._buf) > _MAX_TAG_CHARS:
                    out.append(self._buf)
                    self._buf = ""
                break

            inner = self._buf[1:gt].strip()
            name = _tag_name(inner)
            if name not in _TAGS:
                # 不是提示词标签（"a < b" 这类）：原样吐出这个 '<'
                out.append("<")
                self._buf = self._buf[1:]
                continue

            self._buf = self._buf[gt + 1:]
            if not inner.startswith("/") and name in _SKIP_BLOCKS:
                self._skip_name = name
        return "".join(out)


def _tag_name(raw: str) -> str:
    """'user_query attr="x"' -> 'user_query'；'/response' -> 'response'。"""
    if not raw:
        return ""
    raw = raw.lstrip("/").strip()
    if not raw:
        return ""
    m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", raw)
    return m.group(0).lower() if m else ""
