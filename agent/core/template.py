"""提示词渲染与路由结果解析。

render: 单次正则替换填充 {var}，避免 str.format 误解析模板里的花括号。
parse_route: 解析分类节点输出的 XML（失败兜底 JSON，再失败降级 clarify）。
"""
import json
import os
import re
from functools import cache

_PROMPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prompts")

# 只认「{identifier}」形态的占位符，JSON 片段里的 {"a":1} 不会被误伤
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


@cache
def load_template(name: str) -> str:
    """读取 prompts 目录下的模板（带缓存），name 如 'router.xml'。"""
    with open(os.path.join(_PROMPT_DIR, name), encoding="utf-8") as f:
        return f.read()


def render(name: str, **vars) -> str:
    """把 {key} 占位符替换为对应值；未提供的占位符替换为空串。

    必须单次替换模板本身：逐键 replace 会把「注入值里的 {contexts}」再替换一次
    （用户问题可借此拼进检索片段），末尾清洗占位符的正则也会删掉用户正文里的花括号。
    """
    template = load_template(name)
    return _PLACEHOLDER_RE.sub(
        lambda m: "" if vars.get(m.group(1)) is None else str(vars[m.group(1)]),
        template,
    )


def extract(tag: str, text: str):
    """抽取 <tag>...</tag> 内容，取最后一个匹配（模型可能重复输出）。"""
    m = re.findall(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    return m[-1].strip() if m else None


def parse_route(text: str) -> dict:
    """解析路由输出 {intent, confidence, reason, entities}。

    优先 XML，其次 JSON，都失败降级 clarify。
    """
    intent = extract("intent", text)
    if intent in {"stock", "chat", "off_topic", "clarify"}:
        try:
            conf = float(extract("confidence", text) or 0.5)
        except ValueError:
            conf = 0.5
        return {
            "intent": intent,
            "confidence": conf,
            "reason": extract("reason", text) or "",
            "entities": {
                "symbol": extract("symbol", text) or "",
                "market": extract("market", text) or "",
                "date_range": extract("date_range", text) or "",
            },
        }
    try:
        d = json.loads(text[text.find("{"):text.rfind("}") + 1])
        return {**d, "confidence": float(d.get("confidence", 0.5))}
    except Exception:
        return {"intent": "clarify", "confidence": 0.0, "reason": "parse_failed"}
