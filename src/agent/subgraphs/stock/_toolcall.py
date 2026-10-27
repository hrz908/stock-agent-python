"""tool_call / tool_execute / synthesize 三个节点 + 路由函数。

依赖 _state（工具集 + 绑定 LLM）、_cards（emit_structured）、_fallback（兜底文案）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from agent.config import (
    ANSWER_ENABLE_THINKING,
    MAX_INPUT_TOKENS,
    TOOL_CONCURRENCY,
    TOOL_ROUND_BUDGET,
)
from agent.core.llm import estimate_tokens, get_llm, is_context_overflow, message_chars
from agent.core.sanitize import StreamCleaner
from agent.core.state import AgentState, recent_turns
from agent.core.template import load_template, render
from agent.subgraphs import emit
from agent.subgraphs.stock._cards import _emit_structured
from agent.subgraphs.stock._fallback import (
    OVERFLOW_REPLY,
    _all_failures_graceful_empty,
    _failure_reason,
    _got_data,
    _needs_data_fetch,
    _no_data_fallback,
    _timed_out,
)
from agent.subgraphs.stock._state import (
    _TOOL_LABELS,
    _ensure_tools,
    _tool_bound_llm,
    _tools_by_name,
    fast_tool_calls,
    tool_cache_get,
    tool_cache_put,
)

logger = logging.getLogger("stocka.agent")

# ========== 常量 ==========

_MAX_TOOL_ROUNDS = 3
_MAX_MSG_CHARS = 2000
_MAX_TOOL_CHARS = 6000

# prompt 前字段瘦身
_SLIM_STR_CHARS = 300
_SLIM_LIST_ITEMS = 20

# 有些模型不填 tool_calls，而把函数调用写进正文（见 stock.py 原始注释）
_TEXT_TOOL_RE = re.compile(r"function\s*\n?\s*([A-Za-z_][A-Za-z0-9_]*)")
_TEXT_TOOL_RE2 = re.compile(r"(?:^|\n)\s*([A-Za-z_][A-Za-z0-9_]*)\s*\n\s*\{")
_TRAILING_IDENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*$")


# ========== 上下文裁剪 ==========

def _msg_tokens(msg) -> int:
    c = getattr(msg, "content", "")
    return estimate_tokens(c if isinstance(c, str) else str(c))


def _fit_context(messages: list) -> list:
    """按 token 预算裁剪上下文：先丢历史，再截提示词。"""
    if message_chars(messages) < MAX_INPUT_TOKENS * 0.3:
        return messages
    costs = [_msg_tokens(m) for m in messages]
    total = sum(costs)
    if total <= MAX_INPUT_TOKENS:
        return messages
    out = list(messages)
    idx = 1
    while idx < len(out) - 1 and total > MAX_INPUT_TOKENS:
        total -= costs[idx]
        idx += 1
    out = [out[0]] + out[idx:]
    if total > MAX_INPUT_TOKENS:
        text = out[0].content if isinstance(out[0].content, str) else str(out[0].content)
        out[0] = SystemMessage(content=text[:max(2000, len(text) // 2)] + "\n…[上下文已裁剪]")
    return out


def _slim_value(value):
    if isinstance(value, str):
        return value if len(value) <= _SLIM_STR_CHARS else value[:_SLIM_STR_CHARS] + "…"
    if isinstance(value, list):
        return [_slim_value(v) for v in value[:_SLIM_LIST_ITEMS]]
    if isinstance(value, dict):
        return {k: _slim_value(v) for k, v in value.items()}
    return value


def _slim_tool_results(tool_results: list) -> list:
    slim = []
    for item in tool_results:
        r = item.get("result") or {}
        entry = {"tool": item.get("tool"), "ok": r.get("ok"), "data": _slim_value(r.get("data"))}
        if r.get("error"):
            entry["error"] = r["error"]
        slim.append(entry)
    return slim


# ========== 消息整形 ==========

def _shrink(msg):
    content = getattr(msg, "content", "")
    if isinstance(content, str) and len(content) > _MAX_MSG_CHARS:
        try:
            return msg.model_copy(update={"content": content[:_MAX_MSG_CHARS] + "\n…[已截断]"})
        except Exception:  # noqa: BLE001
            return msg
    return msg


def _recent_history(state: dict, limit: int = 6) -> list:
    return [_shrink(m) for m in recent_turns(state, limit, query=state.get("query"))]


def _apply_rewrite(run_msgs: list, state: dict) -> list:
    """把本轮首条用户消息的正文替换为 rewritten（有值时）。"""
    rewritten = (state.get("rewritten") or "").strip()
    query = (state.get("query") or "").strip()
    if not rewritten or rewritten == query or not run_msgs:
        return run_msgs
    first = run_msgs[0]
    content = first.content if isinstance(first.content, str) else ""
    if content.strip() != query:
        return run_msgs
    try:
        return [first.model_copy(update={"content": rewritten})] + run_msgs[1:]
    except Exception:  # noqa: BLE001
        return run_msgs


def _current_run_messages(state: dict) -> list:
    msgs = list(state.get("messages", []))
    for i in range(len(msgs) - 1, -1, -1):
        if isinstance(msgs[i], HumanMessage):
            head = msgs[i:]
            return _apply_rewrite(head, state)
    return msgs


def _call_rounds(state: dict) -> int:
    """tool_call 节点已执行的轮数 = 本轮消息里 AIMessage 的条数。"""
    return sum(1 for m in _current_run_messages(state) if isinstance(m, AIMessage))


# ========== 文本形态 tool_call 还原 ==========

def _extract_json_object(text: str, start: int) -> str:
    i = text.find("{", start)
    if i < 0:
        return ""
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    return ""


def _iter_json_objects(content: str):
    i = 0
    n = len(content)
    while i < n:
        if content[i] != "{":
            i += 1
            continue
        raw = _extract_json_object(content, i)
        if not raw:
            i += 1
            continue
        yield raw, content[:i]
        i += len(raw)


def _tool_name_in(prefix: str):
    stripped = re.sub(r"```(?:json)?", " ", prefix)
    m = _TRAILING_IDENT_RE.search(stripped)
    if m and m.group(1) in _tools_by_name():
        return m.group(1)
    return None


def _infer_tool_by_args(args: dict):
    if "query" in args:
        return "symbol_search"
    if "peers" in args:
        return "get_peer_compare"
    if "top_n" in args:
        return "get_industry_peers"
    if "start_time" in args or "end_time" in args:
        return "get_stock_news"
    if "code" in args:
        rest = {k for k in args if k != "code"}
        if not rest:
            return "get_quote"
        if rest <= {"days", "adj", "end_date"}:
            return "get_history_daily"
        if "limit" in rest:
            return "get_stock_news"
    return None


def _parse_text_tool_call(content: str):
    if not content or "{" not in content:
        return None, None
    known = _tools_by_name()
    for raw, prefix in _iter_json_objects(content):
        try:
            args = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(args, dict):
            continue
        name = _tool_name_in(prefix)
        if name in known:
            return name, args
        inferred = _infer_tool_by_args(args)
        if inferred:
            return inferred, args
    return None, None


def _normalize_tool_call(ai: AIMessage) -> AIMessage:
    if getattr(ai, "tool_calls", None):
        return ai
    content = ai.content if isinstance(ai.content, str) else ""
    name, args = _parse_text_tool_call(content)
    if not name:
        return ai
    call_id = f"call_{name}_{uuid.uuid4().hex[:8]}"
    logger.info("restore text-form tool call: %s", name)
    return ai.model_copy(update={
        "content": "",
        "tool_calls": [{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    })


# ========== 工具调用策略 ==========

async def _fast_plan(state: dict):
    results = state.get("tool_results") or []
    if any(item.get("tool") != "symbol_search" for item in results):
        return None
    query = state.get("rewritten") or state.get("query") or ""
    return await fast_tool_calls(query, results)


def _pending_nudge(state: dict) -> str:
    """还没取到任何行情数据、且用户要的是数据时，追加强制取数的指令。"""
    if _call_rounds(state) < 1:
        return ""
    results = state.get("tool_results") or []
    codes = []
    for item in results:
        name = item.get("tool")
        res = item.get("result") or {}
        if not res.get("ok"):
            continue
        if name == "symbol_search":
            for row in (res.get("data") or [])[:3]:
                if isinstance(row, dict) and row.get("code"):
                    codes.append(str(row["market"] or "") + str(row["code"]))
    if _got_data(state) or not _needs_data_fetch(state):
        return ""
    hint = "必须用工具取真实数据（行情用 get_quote，参数见工具列表）"
    if codes:
        hint = "已检索到代码：" + "、".join(codes) + "。" + hint
    return hint + "；禁止直接作答，也禁止引用历史消息里的数字（那是上一轮的过期数据）。"


# ========== 节点 ==========

async def tool_call(state: dict) -> dict:
    t0 = time.monotonic()
    prev_ms = int((state.get("timings") or {}).get("tool_call_ms", 0))

    def done(messages: list) -> dict:
        return {
            "messages": messages,
            "timings": {
                "tool_call_ms": prev_ms + int((time.monotonic() - t0) * 1000),
            },
        }

    await _ensure_tools()
    planned = await _fast_plan(state)
    if planned:
        logger.info("fast plan, skip tool-call llm: %s", [c["name"] for c in planned])
        return done([AIMessage(content="", tool_calls=planned)])

    run_msgs = [_shrink(m) for m in _current_run_messages(state)]
    if not run_msgs:
        run_msgs = [HumanMessage(content=state.get("query", "") or "")]
    msgs = [SystemMessage(content=render(
        "stock_toolcall.xml", examples=load_template("toolcall_examples.xml")))] + run_msgs
    nudge = _pending_nudge(state)
    if nudge:
        msgs.append(SystemMessage(content=nudge))
    llm = _tool_bound_llm()
    try:
        ai = await llm.ainvoke(msgs)
    except Exception as e:  # noqa: BLE001
        if not is_context_overflow(e):
            raise
        logger.warning("tool_call context overflow, skip tool round", exc_info=True)
        return done([AIMessage(content="")])
    return done([_normalize_tool_call(ai)])


async def _run_with_budget(tool_calls: list, runner) -> list:
    tasks = [(tc, asyncio.create_task(runner(tc))) for tc in tool_calls]
    task_objs = [t for _, t in tasks]
    budget = TOOL_ROUND_BUDGET
    if budget and budget > 0:
        done_set, pending = await asyncio.wait(task_objs, timeout=budget)
    else:
        done_set, pending = set(task_objs), set()
    if pending:
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    outcomes = []
    for tc, task in tasks:
        if task in done_set and not task.cancelled():
            try:
                outcomes.append(task.result())
                continue
            except Exception as e:  # noqa: BLE001
                logger.warning("tool task failed: %s", e)
        emit("tool_end", {"tool": tc["name"], "ok": False, "ms": int(budget * 1000)})
        outcomes.append((tc["id"], tc["name"], {"ok": False, "error": "timeout"}))
    return outcomes


async def tool_execute(state: dict) -> dict:
    """并发执行 last AIMessage 的所有 tool_calls，收集结构化结果。"""
    t0 = time.monotonic()
    prev_ms = int((state.get("timings") or {}).get("tool_execute_ms", 0))
    msgs = state.get("messages", [])
    ai = msgs[-1] if msgs and isinstance(msgs[-1], AIMessage) else None
    if ai is None or not ai.tool_calls:
        return {}

    by_name = _tools_by_name()
    sem = asyncio.Semaphore(max(1, TOOL_CONCURRENCY))

    async def _run(tc):
        tool = by_name.get(tc["name"])
        args = tc.get("args", {}) or {}
        name = tc["name"]
        label = _TOOL_LABELS.get(name, name)
        if tool is None:
            return tc["id"], name, {"ok": False, "error": f"unknown tool {name}"}
        emit("tool_start", {"tool": name, "label": f"正在获取{label}"})

        cached = tool_cache_get(name, args)
        if cached is not None:
            emit("tool_end", {"tool": name, "ok": bool(cached.get("ok")), "ms": 0})
            return tc["id"], name, cached

        t0_run = time.monotonic()
        async with sem:
            try:
                result = await tool.ainvoke(args)
            except Exception as e:  # noqa: BLE001
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        elapsed_ms = int((time.monotonic() - t0_run) * 1000)
        emit("tool_end", {"tool": name, "ok": bool(result.get("ok")), "ms": elapsed_ms})
        tool_cache_put(name, args, result)
        return tc["id"], name, result

    outcomes = await _run_with_budget(ai.tool_calls, _run)

    tool_messages = []
    tool_results = []
    for tc_id, name, result in outcomes:
        tool_messages.append(ToolMessage(
            content=json.dumps(result, ensure_ascii=False, default=str),
            tool_call_id=tc_id,
            name=name,
        ))
        tool_results.append({"tool": name, "result": result})

    # 知识库工具补 sources 事件
    kb_docs: list[dict] = []
    seen_kb: set[str] = set()
    for item in tool_results:
        if item.get("tool") != "search_knowledge_base":
            continue
        result = item.get("result") or {}
        if not result.get("ok"):
            continue
        for doc in result.get("data") or []:
            cid = str(doc.get("chunk_id") or "")
            key = cid or f"{doc.get('title', '')}|{str(doc.get('content', ''))[:40]}"
            if key in seen_kb:
                continue
            seen_kb.add(key)
            kb_docs.append({
                "title": doc.get("title", ""),
                "chunk_id": cid,
                "score": doc.get("score", 0.0),
            })
    if kb_docs:
        emit("sources", {"docs": kb_docs[:8]})

    return {
        "messages": tool_messages,
        "tool_results": tool_results,
        "timings": {
            "tool_execute_ms": prev_ms + int((time.monotonic() - t0) * 1000),
        },
    }


async def synthesize(state: dict) -> dict:
    t0 = time.monotonic()
    tool_results = state.get("tool_results", []) or []
    fallback = _no_data_fallback(state, tool_results)
    if fallback:
        emit("token", {"delta": fallback})
        return {
            "messages": [AIMessage(content=fallback)],
            "timings": {
                "synth_ttft_ms": 0,
                "synth_ms": int((time.monotonic() - t0) * 1000),
            },
        }

    tr_text = (json.dumps(_slim_tool_results(tool_results), ensure_ascii=False, default=str)
               if tool_results else "（无工具数据）")
    if len(tr_text) > _MAX_TOOL_CHARS:
        tr_text = tr_text[:_MAX_TOOL_CHARS] + "\n…[工具结果已截断]"
    reason = _failure_reason(tool_results)
    if reason:
        tr_text = f"【数据获取失败说明】{reason}\n{tr_text}"
    query = state.get("rewritten") or state.get("query", "") or ""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt = render("stock.xml", now=now, query=query, tool_results=tr_text,
                    examples=load_template("stock_examples.xml"))

    _emit_structured(state, tool_results)

    messages = [SystemMessage(content=prompt)]
    messages.extend(_recent_history(state))
    messages.append(HumanMessage(content=state.get("query", "") or query))
    messages = _fit_context(messages)

    llm = get_llm(streaming=True, temperature=0.4, thinking=ANSWER_ENABLE_THINKING)
    ttft_ms = None
    cleaner = StreamCleaner()
    sent: list[str] = []
    last_usage = None
    try:
        parts = []
        async for chunk in llm.astream(messages):
            if isinstance(chunk.content, str) and chunk.content:
                delta = cleaner.feed(chunk.content)
                if delta:
                    if ttft_ms is None:
                        ttft_ms = int((time.monotonic() - t0) * 1000)
                    sent.append(delta)
                    emit("token", {"delta": delta})
            um = getattr(chunk, "usage_metadata", None)
            if um:
                last_usage = um
            parts.append(chunk)
        tail = cleaner.flush()
        if tail:
            sent.append(tail)
            emit("token", {"delta": tail})
    except Exception as e:  # noqa: BLE001
        if not is_context_overflow(e):
            raise
        logger.warning("synthesize context overflow, fallback to safe reply", exc_info=True)
        emit("token", {"delta": OVERFLOW_REPLY})
        return {
            "messages": [AIMessage(content=OVERFLOW_REPLY)],
            "timings": {
                "synth_ttft_ms": ttft_ms if ttft_ms is not None else 0,
                "synth_ms": int((time.monotonic() - t0) * 1000),
            },
        }
    ai = parts[0] if parts else None
    for p in parts[1:]:
        ai = ai + p
    if ai is not None:
        patch = {"content": "".join(sent)}
        if last_usage:
            patch["usage_metadata"] = last_usage
        ai = ai.model_copy(update=patch)
    total_ms = int((time.monotonic() - t0) * 1000)
    return {
        "messages": [ai if ai is not None else AIMessage(content="")],
        "timings": {
            "synth_ttft_ms": ttft_ms if ttft_ms is not None else total_ms,
            "synth_ms": total_ms,
        },
    }


# ========== 路由 ==========

def route_after_call(state: dict) -> Literal["tool_execute", "tool_call", "synthesize"]:
    msgs = state.get("messages", [])
    last = msgs[-1] if msgs and isinstance(msgs[-1], AIMessage) else None
    if last is not None and getattr(last, "tool_calls", None):
        return "tool_execute" if _call_rounds(state) <= _MAX_TOOL_ROUNDS else "synthesize"
    if (
        _call_rounds(state) < _MAX_TOOL_ROUNDS
        and _needs_data_fetch(state)
        and not _got_data(state)
    ):
        if _all_failures_graceful_empty(state.get("tool_results") or []):
            return "synthesize"
        return "tool_call"
    return "synthesize"


def route_after_execute(state: dict) -> Literal["tool_call", "synthesize"]:
    if _got_data(state):
        return "synthesize"
    if _timed_out(state.get("tool_results") or []):
        return "synthesize"
    if _all_failures_graceful_empty(state.get("tool_results") or []):
        return "synthesize"
    return "tool_call"


def build_stock_subgraph():
    g = StateGraph(AgentState)
    g.add_node("tool_call", tool_call)
    g.add_node("tool_execute", tool_execute)
    g.add_node("synthesize", synthesize)

    g.add_edge(START, "tool_call")
    g.add_conditional_edges("tool_call", route_after_call, {
        "tool_execute": "tool_execute",
        "tool_call": "tool_call",
        "synthesize": "synthesize",
    })
    g.add_conditional_edges("tool_execute", route_after_execute, {
        "tool_call": "tool_call",
        "synthesize": "synthesize",
    })
    g.add_edge("synthesize", END)
    return g.compile(checkpointer=False)
