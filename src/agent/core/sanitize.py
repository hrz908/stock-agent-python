"""流式输出净化：剥掉模型漏出的提示词标签 + 丢弃「过程说明」整行。

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

第二层（2026-10-29 线上截图）：模型会在正文前后交代「知识库暂时超时不可用」
「本轮工具只返回了新闻」「数据缺口：未获取到财务数据」这类过程说明。提示词
已明令禁止，但拦不住所有模型版本，这里再按行首特征整行丢弃；正文中间出现
同一个词不动——用户可能问的就是它。
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


# 「过程说明」行首特征：取数 / 检索状态、数据缺口、语料覆盖、来源与免责交代。
# 只认行首：这些词出现在正文中间时往往是用户问的主题（「知识库是什么」），
# 宁可漏杀不可误杀，模型层面的约束由 prompts 负责。
_META_LINE_OPENERS = (
    # 检索 / 取数状态
    "知识库暂时", "知识库超时", "知识库不可用", "知识库没有", "知识库未",
    "知识库检索失败", "知识库检索超时", "知识库检索不到",
    "本轮工具", "本轮数据", "本轮检索", "本轮没有", "本轮未",
    "调用失败", "接口超时", "检索超时", "检索失败", "工具调用失败", "工具返回",
    # 数据缺口交代
    "数据缺口", "数据缺失", "数据未获取", "未获取到", "未取到", "没有取到",
    "未返回", "没有返回",
    # 语料 / 资料覆盖交代
    "资料中未", "资料未提及", "资料未覆盖", "资料中没有",
    "语料库里", "语料库中", "语料范围", "语料边界",
    # 免责声明
    "以上仅为", "以上内容仅", "以上均仅", "以上只是",
    "不构成投资建议", "仅供参考",
    # 自述式开场
    "我先按", "抱歉，这轮", "抱歉，本次",
)

# 分点前缀：判定前先剥掉，否则「- 数据缺口：…」会逃过行首匹配
_LINE_BULLETS = ("- ", "* ", "· ", "• ", "— ")


def _classify_line(line: str) -> str:
    """'meta' 说明行 / 'maybe' 还判断不出 / 'normal' 正常正文。"""
    head = line.lstrip(" \t")
    for bullet in _LINE_BULLETS:
        # 分点符号可能被拆到两块里：先只收到「-」时判不出下一行是不是说明行
        if head == bullet.strip():
            return "maybe"
        if head.startswith(bullet):
            head = head[len(bullet):]
            break
    head = head.lstrip(" \t")
    if not head:
        return "maybe"
    for opener in _META_LINE_OPENERS:
        if head.startswith(opener):
            return "meta"
    for opener in _META_LINE_OPENERS:
        if opener.startswith(head):
            return "maybe"
    return "normal"


class _MetaLineFilter:
    """按行丢弃「过程说明」，其余文本原样透传。

    背景（2026-10-29 线上截图）：模型会在正文前交代「知识库暂时超时不可用，我先按
    通用常识说明」，或在末尾补一条「数据缺口：本轮未获取到财务数据」。这类话术
    独立成行，整行丢弃既不伤到有数据的那部分，也不用在句子中间切一刀。

    流式约束：只在行首判定，且只有「还可能命中」时才缓冲——正常回答第一个字就
    放行，不压首字延迟；命中后吞到该行结束。
    """

    def __init__(self) -> None:
        self._buf = ""
        # 当前行已判定为正常：后续字符直通，只盯着换行
        self._inline = False
        # 已判定为说明行：吞到换行为止
        self._dropping = False

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        if self._dropping:
            nl = chunk.find("\n")
            if nl < 0:
                return ""
            self._dropping = False
            self._inline = False
            return self.feed(chunk[nl + 1:])
        if self._inline:
            nl = chunk.find("\n")
            if nl < 0:
                return chunk
            self._inline = False
            return chunk[:nl + 1] + self.feed(chunk[nl + 1:])
        self._buf += chunk
        return self._drain()

    def flush(self) -> str:
        if self._dropping:
            self._buf = ""
            self._dropping = False
            return ""
        if _classify_line(self._buf) == "meta":
            self._buf = ""
            return ""
        out, self._buf = self._buf, ""
        return out

    def _drain(self) -> str:
        out: list[str] = []
        while self._buf:
            nl = self._buf.find("\n")
            line = self._buf if nl < 0 else self._buf[:nl]
            kind = _classify_line(line)
            if kind == "meta":
                if nl < 0:
                    # 说明行还没写完：先整行吞掉，等换行确认边界
                    self._buf = ""
                    self._dropping = True
                    break
                self._buf = self._buf[nl + 1:]
                continue
            if kind == "maybe" and nl < 0:
                # 如只收到「知识」：攒够能判定再放行
                break
            if nl < 0:
                out.append(self._buf)
                self._buf = ""
                self._inline = True
                break
            out.append(self._buf[:nl + 1])
            self._buf = self._buf[nl + 1:]
        return "".join(out)


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
        # 过程说明按行丢弃；必须在 _emit 之前跑，否则说明行会让 _started
        # 提前置位，丢弃后留下的空行就裁不掉了
        self._meta = _MetaLineFilter()

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        # 快路径：无残留、无跳过态、本段无 '<'，直接过说明行过滤（绝大多数 token 走这里）
        if not self._buf and not self._skip_name and "<" not in chunk:
            return self._emit(self._meta.feed(chunk))
        self._buf += chunk
        return self._emit(self._meta.feed(self._drain()))

    def flush(self) -> str:
        """流结束：把残留缓冲按「像不像半个标签」决定去留。"""
        if self._skip_name:
            # 块没闭合就结束了：这段内容本来就不该给用户看
            self._buf = ""
            self._skip_name = ""
            self._closing = False
            return self._emit(self._meta.flush())
        tail = self._buf
        self._buf = ""
        if tail.startswith("<"):
            # 只有「像半个已知标签」才丢；「若 PE < 10」这类正文必须原样保留
            name = _tag_name(tail[1:].split(">")[0].strip())
            if name and any(t.startswith(name) for t in _TAGS):
                tail = ""
        return self._emit(self._meta.feed(tail) + self._meta.flush())

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
