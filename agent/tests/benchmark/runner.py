"""批量执行器：把题库逐条发给 Agent，收集回答与全过程事件。

两种模式：
- inproc : 进程内直接跑 LangGraph 主图（默认）。即时反映工作区代码/提示词改动，
           不需要重启容器；Windows 下强制 SelectorEventLoop（psycopg 要求）。
- http   : POST 到运行中的 agent 服务（默认 http://localhost:8000/run/stream），
           按 ndjson 解析事件流；最终 intent 从 postgres 的 agent_runs 审计表回查。

每个用例一个独立 thread_id，多轮用例在同一 thread 上按序下发。
结果写入 JSONL（每行一个用例），供 evaluate / report 消费。
"""
import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from .cases import Case

AGENT_DIR = Path(__file__).resolve().parents[2]  # .../agent
REPO_DIR = AGENT_DIR.parent                        # .../new-projects


def load_env_file(path) -> dict:
    """解析 KEY=VALUE 文件（无第三方依赖）；不存在返回空 dict。"""
    path = Path(path)
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def apply_inproc_env(env_file=None, verbose=False) -> dict:
    """in-process 模式的环境准备。

    1. 先加载 server/.env（LLM key、模型名等），已存在的环境变量优先；
    2. 覆盖只在宿主机 direct 跑时才成立的项：
       - GO_SERVER_URL -> 127.0.0.1:8080（.env 里是 host.docker.internal，宿主不可解析）
       - NO_PROXY=*（本机系统代理会拦 akshare/东财直连；LLM 走 SiliconFlow 实测直连可用）
       - TOOL_CACHE_TTL=0（基准要测真实上游延迟，关掉工具结果缓存）
    3. DATABASE_URL 若指向 docker 内网主机名（postgres），改回 localhost。
    """
    env = load_env_file(env_file or (REPO_DIR / "server" / ".env"))
    applied = {}
    for k, v in env.items():
        if k not in os.environ:
            os.environ[k] = v
            applied[k] = v
    overrides = {
        "GO_SERVER_URL": "http://127.0.0.1:8080",
        "NO_PROXY": "*",
        "no_proxy": "*",
        "TOOL_CACHE_TTL": "0",
    }
    for k, v in overrides.items():
        os.environ[k] = v
        applied[k] = v
    db = os.environ.get("DATABASE_URL", "")
    if "@postgres:" in db or "@redis:" in db:
        os.environ["DATABASE_URL"] = re.sub(r"@postgres:", "@localhost:", db)
        os.environ["REDIS_URL"] = re.sub(r"@redis:", "@localhost:", os.environ.get("REDIS_URL", ""))
        applied["DATABASE_URL"] = os.environ["DATABASE_URL"]
    if verbose:
        print("[env] loaded keys:", ", ".join(sorted(applied)))
    return applied


def _tool_digest(tool_results: list, max_chars: int = 1200) -> list:
    """把工具结果压成评委可读的摘要：名字 + ok + 关键字段 + 截断的原始数据。"""
    digest = []
    for item in tool_results or []:
        name = item.get("tool", "?")
        res = item.get("result") or {}
        entry = {"tool": name, "ok": bool(res.get("ok"))}
        if not res.get("ok"):
            entry["error"] = str(res.get("error", ""))[:200]
        else:
            data = res.get("data")
            entry["data_head"] = str(data)[:max_chars]
        digest.append(entry)
    return digest


async def run_case_inproc(case: Case, graph, run_id: str) -> dict:
    """进程内跑一个用例（含多轮），返回结构化结果。"""
    from langchain_core.messages import HumanMessage

    from core.state import initial_state

    thread_id = f"bench-{run_id}-{case.id}"
    config = {"configurable": {"thread_id": thread_id}}
    turns_out = []

    for query in case.turns:
        t0 = time.monotonic()
        inp = initial_state(user_id="1", thread_id=thread_id, query=query)
        inp["messages"] = [HumanMessage(content=query)]

        text_parts, cards, tables, sources, errors = [], [], [], [], []
        tool_events = {}
        stage_timings = {}
        final_intent, final_conf = "", 0.0
        ttft_ms = None
        first_token_ms = None
        first_event_ms = None

        async for item in graph.astream(
            inp, config=config,
            stream_mode=["custom", "updates"], subgraphs=True,
        ):
            if isinstance(item, tuple) and len(item) == 3:
                _ns, mode, data = item
            elif isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str):
                mode, data = item
            else:
                mode, data = "custom", item
            if mode == "custom" and isinstance(data, dict) and "event" in data:
                ev, payload = data["event"], data.get("data", {})
                if first_event_ms is None:
                    first_event_ms = int((time.monotonic() - t0) * 1000)
                # 与线上 agent_runs.ttft_ms 对齐：token / card / table 都算“看到答案”
                if ev in {"token", "card", "table"} and ttft_ms is None:
                    ttft_ms = int((time.monotonic() - t0) * 1000)
                if ev == "token":
                    if first_token_ms is None:
                        first_token_ms = int((time.monotonic() - t0) * 1000)
                    text_parts.append(payload.get("delta", ""))
                elif ev == "card":
                    cards.append(payload.get("type", "?"))
                elif ev == "table":
                    tables.append(payload.get("type", "?"))
                elif ev == "sources":
                    sources.append(payload)
                elif ev == "tool_start":
                    name = payload.get("tool", "?")
                    tool_events.setdefault(name, {"name": name, "ms": None})
                elif ev == "tool_end":
                    name = payload.get("tool", "?")
                    item = tool_events.setdefault(name, {"name": name, "ms": None})
                    item["ms"] = payload.get("ms")
                    item["ok"] = payload.get("ok")
                elif ev == "error":
                    errors.append(str(payload.get("message", ""))[:300])
            elif mode == "updates" and isinstance(data, dict):
                for node_update in data.values():
                    if isinstance(node_update, dict):
                        if isinstance(node_update.get("timings"), dict):
                            stage_timings.update(node_update["timings"])
                        if node_update.get("intent"):
                            final_intent = node_update["intent"]
                        if node_update.get("confidence") is not None:
                            final_conf = node_update["confidence"]
                        if node_update.get("error"):
                            errors.append(str(node_update["error"])[:300])

        # 回查该轮最终状态（拿 tool_results / intent / confidence 的权威值）
        try:
            snap = await graph.aget_state(config)
            values = (snap.values if snap else None) or {}
        except Exception:  # noqa: BLE001 - 状态回查失败不阻断收集
            values = {}
        if values.get("intent"):
            final_intent = values["intent"]
        if values.get("confidence") is not None:
            final_conf = values["confidence"]
        if isinstance(values.get("timings"), dict):
            stage_timings = {**stage_timings, **values["timings"]}
        tr = values.get("tool_results") or []

        # 正文以最终状态里的最后一条 AIMessage 为准：post 节点会把免责声明
        # 追加到该消息上，而 token 流里没有（流式结束才追加）。
        # token 拼接文本仅作兜底（确定性回答不走流式时）。
        text = ""
        for m in reversed(values.get("messages") or []):
            if type(m).__name__ == "AIMessage" and isinstance(m.content, str) and m.content.strip():
                text = m.content
                break
        if not text:
            text = "".join(text_parts)

        turns_out.append({
            "query": query,
            "intent": final_intent,
            "confidence": final_conf,
            "tools": _tool_digest(tr),
            "text": text,
            "cards": cards,
            "tables": tables,
            "sources": len(sources),
            "errors": errors,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "first_event_ms": first_event_ms,
            "ttft_ms": ttft_ms,
            "first_token_ms": first_token_ms,
            "timings": stage_timings,
            "tool_events": list(tool_events.values()),
        })

    last = turns_out[-1] if turns_out else {}
    return {
        "id": case.id,
        "category": case.category,
        "difficulty": case.difficulty,
        "mode": "inproc",
        "ts": datetime.now().isoformat(timespec="seconds"),
        "turns": turns_out,
        "final": {
            "text": last.get("text", ""),
            "intent": last.get("intent", ""),
            "tools": last.get("tools", []),
            "cards": last.get("cards", []) + last.get("tables", []),
        },
        "all_tools": sorted({t["tool"] for tn in turns_out for t in tn["tools"]}),
        "all_cards": sorted({c for tn in turns_out for c in tn["cards"] + tn["tables"]}),
        "errors": [e for tn in turns_out for e in tn["errors"]],
        "elapsed_ms_total": sum(tn["elapsed_ms"] for tn in turns_out),
    }


async def run_case_http(case: Case, base_url: str, run_id: str, timeout: float = 180.0) -> dict:
    """HTTP 模式跑一个用例（事件按 ndjson 解析）。"""
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    thread_id = f"bench-{run_id}-{case.id}"
    turns_out = []

    for query in case.turns:
        t0 = time.monotonic()
        payload = json.dumps({
            "thread_id": thread_id, "query": query, "user_id": "1",
        }).encode("utf-8")
        req = urllib.request.Request(
            base_url.rstrip("/") + "/run/stream", data=payload,
            headers={"Content-Type": "application/json"})
        text_parts, cards, tables, sources, errors = [], [], [], [], []
        tool_events = {}
        ttft_ms = None
        first_token_ms = None
        first_event_ms = None
        try:
            with opener.open(req, timeout=timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    ev = obj.get("event")
                    data = obj.get("data", {})
                    if first_event_ms is None:
                        first_event_ms = int((time.monotonic() - t0) * 1000)
                    if ev in {"token", "card", "table"} and ttft_ms is None:
                        ttft_ms = int((time.monotonic() - t0) * 1000)
                    if ev == "token":
                        if first_token_ms is None:
                            first_token_ms = int((time.monotonic() - t0) * 1000)
                        text_parts.append(data.get("delta", ""))
                    elif ev == "card":
                        cards.append(data.get("type", "?"))
                    elif ev == "table":
                        tables.append(data.get("type", "?"))
                    elif ev == "sources":
                        sources.append(data)
                    elif ev == "tool_end":
                        name = data.get("tool", "?")
                        item = tool_events.setdefault(name, {"name": name, "ms": None})
                        item["ms"] = data.get("ms")
                        item["ok"] = data.get("ok")
                    elif ev == "error":
                        errors.append(str(data.get("message", ""))[:300])
        except Exception as e:  # noqa: BLE001 - 单条失败不阻断整批
            errors.append(f"{type(e).__name__}: {e}")
        turns_out.append({
            "query": query,
            "intent": "",  # HTTP 模式由调用方从 agent_runs 回填
            "confidence": None,
            "tools": _tool_events_list(tool_events),
            "text": "".join(text_parts),
            "cards": cards,
            "tables": tables,
            "sources": len(sources),
            "errors": errors,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "first_event_ms": first_event_ms,
            "ttft_ms": ttft_ms,
            "first_token_ms": first_token_ms,
            "timings": {},
            "tool_events": list(tool_events.values()),
        })

    last = turns_out[-1] if turns_out else {}
    return {
        "id": case.id,
        "category": case.category,
        "difficulty": case.difficulty,
        "mode": "http",
        "ts": datetime.now().isoformat(timespec="seconds"),
        "thread_id": thread_id,
        "turns": turns_out,
        "final": {
            "text": last.get("text", ""),
            "intent": "",
            "tools": last.get("tools", []),
            "cards": last.get("cards", []) + last.get("tables", []),
        },
        "all_tools": sorted({
            t["tool"] for tn in turns_out for t in (tn.get("tools") or []) if t.get("tool")
        }),
        "all_cards": sorted({c for tn in turns_out for c in tn["cards"] + tn["tables"]}),
        "errors": [e for tn in turns_out for e in tn["errors"]],
        "elapsed_ms_total": sum(tn["elapsed_ms"] for tn in turns_out),
    }


def _tool_events_list(tool_events: dict) -> list:
    """HTTP 模式：tool_end 事件 -> evaluate 需要的 tools 列表。

    inproc 模式从 state.tool_results 拿工具名；HTTP 模式没有 state，只能从
    tool_start/tool_end 事件还原。evaluate 的 tools_any/tools_all 统一读 t["tool"]。
    """
    return [
        {"tool": item.get("name"), "ok": item.get("ok"), "ms": item.get("ms")}
        for item in tool_events.values()
        if item.get("name")
    ]


def backfill_intent_http(results: list, dsn: str) -> None:
    """HTTP 模式：从 agent_runs 审计表按 thread_id 回填 intent/confidence。

    agent_runs 的主键是 run_id、没有 id 列；按 created_at 升序取该 thread 的
    全部审计行，再按轮次顺序回填，多轮用例的第一轮也能拿到 intent。
    """
    import psycopg
    with psycopg.connect(dsn) as conn:
        for r in results:
            tid = r.get("thread_id")
            if not tid:
                continue
            rows = conn.execute(
                "SELECT intent, confidence FROM agent_runs "
                "WHERE thread_id = %s ORDER BY created_at ASC",
                (tid,),
            ).fetchall()
            if not rows:
                continue
            for turn, row in zip(r.get("turns") or [], rows, strict=False):
                turn["intent"] = row[0] or ""
                turn["confidence"] = row[1]
            r["final"]["intent"] = rows[-1][0] or ""


async def run_batch(cases: list, mode: str = "inproc", run_id: str = None,
                    concurrency: int = 4, base_url: str = "http://localhost:8000",
                    dsn: str = None, progress=None) -> list:
    """并发跑一批用例，返回结果列表（保持用例顺序）。"""
    run_id = run_id or datetime.now().strftime("%m%d%H%M%S")
    sem = asyncio.Semaphore(max(1, concurrency))
    graph = None

    if mode == "inproc":
        from langgraph.checkpoint.memory import MemorySaver

        from core.graph import build_graph
        graph = build_graph(MemorySaver())

    async def _one(case: Case):
        async with sem:
            if mode == "inproc":
                res = await run_case_inproc(case, graph, run_id)
            else:
                res = await run_case_http(case, base_url, run_id)
            if progress:
                real_err = [e for e in res["errors"] if "guardrail_blocked" not in e]
                flag = "OK" if res["final"]["text"].strip() and not real_err else "BAD"
                progress(f"[{flag}] {case.id} {case.category}/{case.difficulty} "
                         f"{res['elapsed_ms_total'] / 1000:.1f}s cards={res['all_cards']}")
            return res

    results = await asyncio.gather(*[_one(c) for c in cases])
    results = list(results)
    if mode == "http":
        try:
            backfill_intent_http(results, dsn or os.environ.get(
                "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/stock_agent"))
        except Exception as e:  # noqa: BLE001 - 审计回填失败不影响主结果
            print(f"[warn] intent backfill failed: {e}")
    return results


def save_results(results: list, path) -> None:
    """结果写 JSONL（一行一个用例）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def load_results(path) -> list:
    out = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
