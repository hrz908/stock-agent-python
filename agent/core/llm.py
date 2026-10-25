"""LLM 工厂：统一走 OpenAI 兼容接口，base_url / api_key 由 config 提供。

大模型（get_llm）用于最终回答生成，小模型（get_small_llm）用于改写、分类等轻量任务。
"""
from langchain_openai import ChatOpenAI

from config import (
    LLM_MAX_RETRIES,
    LLM_MODEL,
    LLM_TIMEOUT,
    OPENAI_API_BASE,
    OPENAI_API_KEY,
    SMALL_LLM_TIMEOUT,
    SMALL_MODEL,
)

# 进程内复用模型实例：ChatOpenAI 每次实例化都会新建一个 httpx 客户端（含连接池），
# 而一轮对话至少经过 rewrite / classify / 生成三次调用——每次重建 TLS 纯属浪费。
# 实例是无状态的（消息都通过 invoke 传入），按
# (模型, streaming, temperature, timeout, max_retries) 缓存。
_LLM_CACHE: dict = {}


def _cached(
    model: str,
    streaming: bool,
    temperature: float,
    timeout: float,
    max_retries: int,
) -> ChatOpenAI:
    key = (
        model, bool(streaming), round(float(temperature), 2),
        round(float(timeout), 2), int(max_retries),
    )
    llm = _LLM_CACHE.get(key)
    if llm is None:
        llm = ChatOpenAI(
            model=model,
            openai_api_base=OPENAI_API_BASE,
            openai_api_key=OPENAI_API_KEY,
            streaming=streaming,
            temperature=temperature,
            timeout=timeout,
            # OpenAI SDK 默认重试 2 次；不显式收口会让一次上游卡顿变成 60s+
            max_retries=max_retries,
        )
        _LLM_CACHE[key] = llm
    return llm


def get_llm(
    streaming: bool = True,
    temperature: float = 0.7,
    timeout: float | None = None,
    max_retries: int | None = None,
) -> ChatOpenAI:
    """主力对话模型：默认流式，温度略高以保证回答自然。

    timeout 是单次请求硬上限：工具链有 8s 兜底，LLM 生成之前没有，
    不设会让整个事件流无限期挂住（Go 网关 runTimeout 到点砍流，用户只看到断流、没有解释）。
    tool_call 等非流式决策可显式传更短的 timeout。
    """
    return _cached(
        LLM_MODEL,
        streaming,
        temperature,
        LLM_TIMEOUT if timeout is None else timeout,
        LLM_MAX_RETRIES if max_retries is None else max_retries,
    )


def get_small_llm(
    streaming: bool = False,
    temperature: float = 0.0,
    timeout: float | None = None,
    max_retries: int | None = None,
) -> ChatOpenAI:
    """小模型：用于指代消解、意图分类、滚动摘要，温度 0 追求确定性。

    默认 30s 超时：小模型只处理短输入，等满 60s 只会放大长尾。
    """
    return _cached(
        SMALL_MODEL,
        streaming,
        temperature,
        SMALL_LLM_TIMEOUT if timeout is None else timeout,
        LLM_MAX_RETRIES if max_retries is None else max_retries,
    )


# tiktoken 编码器首次构建要读完整词表，进程内复用同一个实例；
# 未安装或加载失败时保持 None，走字符估算
_ENCODER = None
_ENCODER_READY = False


def _encoder():
    global _ENCODER, _ENCODER_READY
    if not _ENCODER_READY:
        _ENCODER_READY = True
        try:
            import tiktoken

            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001 - 没装 tiktoken 是允许的
            _ENCODER = None
    return _ENCODER


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：优先 tiktoken，失败按字符数估算。"""
    if not text:
        return 0
    enc = _encoder()
    if enc is None:
        # 中英混合下 ~2 char/token 的保守估计
        return max(1, len(text) // 2)
    try:
        return len(enc.encode(text))
    except Exception:  # noqa: BLE001
        return max(1, len(text) // 2)


def message_chars(messages: list) -> int:
    """消息正文的字符总数，用作 token 的廉价下界。

    最坏情况（生僻字/emoji）约 3 token 一个字，所以 chars < 阈值 × 0.3 时
    可以确定没超预算，直接跳过 tiktoken 编码——预算判定每轮要跑好几次，
    省下的都是纯 CPU。
    """
    total = 0
    for m in messages:
        c = getattr(m, "content", "")
        total += len(c) if isinstance(c, str) else len(str(c))
    return total


def count_message_tokens(messages: list) -> int:
    """统计一组消息的 token 总量（发送前的上下文预算用）。

    用 join 一次拼好再编码：逐条 += 在长会话里是 O(n²) 的字符串拷贝，
    而预算判定在 guardrail / synthesize / post 里每轮都要跑。
    """
    parts = []
    for m in messages:
        c = getattr(m, "content", "")
        parts.append(c if isinstance(c, str) else str(c))
    return estimate_tokens("".join(parts))


def is_context_overflow(exc: Exception) -> bool:
    """判断异常是否为「上下文超长」。

    上游 OpenAI 兼容接口输入超限时返回 400，例如：
      'number of input tokens (301046) has exceeded max_seq_len (163840) limit'
    这类错误冒泡出去整条回答就是空的（只剩一个 error 事件），必须识别后就地降级。
    """
    msg = str(exc).lower()
    return (
        "max_seq_len" in msg
        or "context length" in msg
        or "maximum context" in msg
        or "too many tokens" in msg
        or "reduce the length" in msg
    )
