"""批量执行器：把题库逐条发给 Agent，收集回答与全过程事件。

两种模式：
- inproc : 进程内直接跑 LangGraph 主图（默认）。即时反映工作区代码/提示词改动，
           不需要重启容器；Windows 下强制 SelectorEventLoop（psycopg 要求）。
           只适合功能调试：不过网关、无限流、无 SSE，耗时数字不能当线上性能。
- http   : 打生产链路 POST /api/v1/agent/stream（JWT + SSE），默认
           http://127.0.0.1:8080。多 uid 摊开单用户限流（qps=2 / concurrent=3），
           最终 intent / 阶段耗时 / token 从 postgres 的 agent_runs 审计表回填。

每个用例一个独立 session（thread_id 由网关拼成 default:{uid}:{session_id}），
多轮用例在同一 thread 上按序下发。
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
REPO_DIR = AGENT_DIR.parent                        # .../stock-agent-rag
# 容器内跑（benchmark / 检索评测）时 DATABASE_URL 的 postgres 主机名就是对的，
# 不能按宿主直连口径改成 localhost
IN_CONTAINER = Path("/.dockerenv").exists()


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
       仅在宿主机直跑时成立；容器内保留 compose 注入的服务名。
    """
    env = load_env_file(env_file or (REPO_DIR / "server" / ".env"))
    applied = {}
    for k, v in env.items():
        if k not in os.environ:
            os.environ[k] = v
            applied[k] = v
    overrides = {
        "NO_PROXY": "*",
        "no_proxy": "*",
        "TOOL_CACHE_TTL": "0",
    }
    if not IN_CONTAINER:
        overrides["GO_SERVER_URL"] = "http://127.0.0.1:8080"
    for k, v in overrides.items():
        os.environ[k] = v
        applied[k] = v
    db = os.environ.get("DATABASE_URL", "")
    if not IN_CONTAINER and ("@postgres:" in db or "@redis:" in db):
        os.environ["DATABASE_URL"] = re.sub(r"@postgres:", "@localhost:", db)
        os.environ["REDIS_URL"] = re.sub(r"@redis:", "@localhost:", os.environ.get("REDIS_URL", ""))
        applied["DATABASE_URL"] = os.environ["DATABASE_URL"]
    if verbose:
        print("[env] loaded keys:", ", ".join(sorted(applied)))
    return applied


def apply_env_file(env_file=None, verbose=False) -> dict:
    """只把 server/.env 灌进进程环境，不做宿主直连覆盖（http 模式用）。

    DSN 是网关口径的键名，agent 侧读 DATABASE_URL；这里补一次映射，
    否则 harness 连不上业务库，agent_runs 回填会静默失败。
    """
    env = load_env_file(env_file or (REPO_DIR / "server" / ".env"))
    applied = {}
    for k, v in env.items():
        if k not in os.environ:
            os.environ[k] = v
            applied[k] = v
    dsn = os.environ.get("DSN", "")
    if dsn and not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = dsn.replace("postgres://", "postgresql://", 1)
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

    from agent.core.state import initial_state

    thread_id = f"bench-{run_id}-{case.id}"
    config = {"configurable": {"thread_id": thread_id}}
    turns_out = []

    for query in case.turns:
        t0 = time.monotonic()
        inp = initial_state(user_id="1", thread_id=thread_id, query=query)
        inp["messages"] = [HumanMessage(content=query)]

        text_parts, cards, tables, sources, errors = [], [], [], [], []
        card_data = []
        source_docs = []
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
                    # 保留结构化数字：evaluate 的数字一致性检查只认卡片里的真值
                    card_data.append({"type": payload.get("type"), "payload": payload.get("payload")})
                elif ev == "table":
                    tables.append(payload.get("type", "?"))
                    card_data.append({"type": payload.get("type"), "payload": payload.get("payload")})
                elif ev == "sources":
                    sources.append(payload)
                    source_docs.extend(payload.get("docs") or [])
                elif ev == "tool_start":
                    name = payload.get("tool", "?")
                    tool_events.setdefault(name, {"name": name, "ms": None})
                elif ev == "tool_end":
                    name = payload.get("tool", "?")
                    item = tool_events.setdefault(name, {"name": name, "ms": None})
                    item["ms"] = payload.get("ms")
                    item["ok"] = payload.get("ok")
                elif ev == "error":
                    # 网关的 error 帧用 msg/code，agent 原生用 message，三种都兜住
                    detail = (payload.get("message") or payload.get("msg")
                              or payload.get("code") or "error（无消息）")
                    errors.append(str(detail)[:300])
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
            "card_data": card_data,
            "sources": len(sources),
            "source_docs": source_docs,
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


def _safe_session_id(run_id: str, case_id: str) -> str:
    """网关只放行 [A-Za-z0-9_-] 且长度 <= 64；这里兜底做一次清洗。

    先保证 case_id 完整，再按剩余长度截 run_id —— 否则长 tag 会把用例 id 截掉，
    同批用例的 session 撞在一起，多轮用例互相污染上下文。
    """
    case_part = re.sub(r"[^0-9A-Za-z_-]", "-", case_id)[:24]
    run_part = re.sub(r"[^0-9A-Za-z_-]", "-", run_id)
    keep = max(1, 64 - len(case_part) - 8)
    return f"bench-{run_part[:keep]}-{case_part}"[:64]


async def run_case_http(
    case: Case,
    client,
    base_url: str,
    run_id: str,
    user_id: int,
    token: str,
    session_id: str,
) -> dict:
    """HTTP 模式跑一个用例：走生产 SSE 契约（/api/v1/agent/stream + JWT）。"""
    from . import sse

    thread_id = f"default:{user_id}:{session_id}"
    turns_out = []

    for query in case.turns:
        t0 = time.monotonic()
        text_parts, cards, tables, sources, errors = [], [], [], [], []
        card_data: list = []
        source_docs: list = []
        tool_events: dict = {}
        usage: dict = {}
        ttft_ms = None
        first_token_ms = None
        first_event_ms = None

        try:
            async for ev, payload in sse.agent_stream(
                client, base_url, token, query, session_id
            ):
                data = payload if isinstance(payload, dict) else {}
                if first_event_ms is None:
                    first_event_ms = int((time.monotonic() - t0) * 1000)
                # 与 agent_runs 口径一致：token / card / table 才算“看到答案”
                if ev in {"token", "card", "table"} and ttft_ms is None:
                    ttft_ms = int((time.monotonic() - t0) * 1000)
                if ev == "token":
                    if first_token_ms is None:
                        first_token_ms = int((time.monotonic() - t0) * 1000)
                    text_parts.append(str(data.get("delta", "")))
                elif ev == "card":
                    cards.append(str(data.get("type", "?")))
                    card_data.append({"type": data.get("type"), "payload": data.get("payload")})
                elif ev == "table":
                    tables.append(str(data.get("type", "?")))
                    card_data.append({"type": data.get("type"), "payload": data.get("payload")})
                elif ev == "sources":
                    sources.append(data)
                    source_docs.extend(data.get("docs") or [])
                elif ev == "tool_start":
                    name = str(data.get("tool", "?"))
                    tool_events.setdefault(name, {"name": name, "ms": None})
                elif ev == "tool_end":
                    name = str(data.get("tool", "?"))
                    item = tool_events.setdefault(name, {"name": name, "ms": None})
                    item["ms"] = data.get("ms")
                    item["ok"] = data.get("ok")
                elif ev == "done":
                    if isinstance(data.get("usage"), dict):
                        usage = data["usage"]
                elif ev == "error":
                    # 网关的 error 帧用 msg/code，agent 原生用 message，三种都兜住
                    detail = (data.get("message") or data.get("msg")
                              or data.get("code") or "error（无消息）")
                    errors.append(str(detail)[:300])
        except sse.StreamHttpError as exc:
            # 401 大概率是 JWT_SECRET 不一致；429 是单 uid 限流（--users 调大）
            detail = exc.body
            try:
                envelope = json.loads(exc.body)
                detail = envelope.get("message") or envelope.get("code") or exc.body
            except ValueError:
                pass
            errors.append(f"http_{exc.status_code}: {detail}"[:300])
        except Exception as e:  # noqa: BLE001 - 单条失败不阻断整批
            errors.append(f"{type(e).__name__}: {e}")

        turns_out.append({
            "query": query,
            "intent": "",  # HTTP 模式由 backfill_http 从 agent_runs 回填
            "confidence": None,
            "tools": _tool_events_list(tool_events),
            "text": "".join(text_parts),
            "cards": cards,
            "tables": tables,
            "card_data": card_data,
            "sources": len(sources),
            "source_docs": source_docs,
            "errors": errors,
            "usage": usage,
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
        "base_url": base_url,
        "user_id": user_id,
        "session_id": session_id,
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


_AUDIT_SQL = """
SELECT thread_id, intent, confidence, tokens_in, tokens_out,
       ttft_ms, first_event_ms, total_ms, tool_calls, error_code, timings
FROM agent_runs
WHERE thread_id = ANY(%s)
ORDER BY thread_id, created_at ASC
"""


def _apply_audit_turn(turn: dict, row: tuple) -> None:
    """把一行审计记录落到某一轮：intent/confidence + 服务端耗时 + token。"""
    turn["intent"] = row[1] or ""
    turn["confidence"] = row[2]
    turn["server"] = {
        "tokens_in": row[3],
        "tokens_out": row[4],
        "ttft_ms": row[5],
        "first_event_ms": row[6],
        "total_ms": row[7],
        "tool_calls": row[8],
        "error_code": row[9],
    }
    timings = row[10]
    if isinstance(timings, str):
        try:
            timings = json.loads(timings)
        except ValueError:
            timings = {}
    if isinstance(timings, dict) and timings:
        turn["timings"] = timings
    if row[9]:
        turn.setdefault("errors", []).append(str(row[9]))


def backfill_http(results: list, dsn: str, wait_s: float = 3.0) -> None:
    """HTTP 模式：从 agent_runs 回填 intent / 阶段耗时 / token。

    审计 INSERT 在 done 帧之后由后台任务执行，客户端收完流时可能还没落库，
    所以按 thread 轮询等待（最多 wait_s 秒），行数够了就提前返回。
    agent_runs 没有 id 列，按 created_at 升序把行对齐到 turns。
    """
    thread_ids = [r["thread_id"] for r in results if r.get("thread_id")]
    if not thread_ids:
        return

    import psycopg

    rows_by_thread: dict = {}
    deadline = time.monotonic() + wait_s
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        while True:
            rows_by_thread = {}
            for row in conn.execute(_AUDIT_SQL, (thread_ids,)).fetchall():
                rows_by_thread.setdefault(row[0], []).append(row)
            complete = all(
                len(rows_by_thread.get(r["thread_id"], [])) >= len(r.get("turns") or [])
                for r in results
            )
            if complete or time.monotonic() >= deadline:
                break
            time.sleep(0.2)

    for r in results:
        rows = rows_by_thread.get(r.get("thread_id"), [])
        if not rows:
            continue
        for turn, row in zip(r.get("turns") or [], rows, strict=False):
            _apply_audit_turn(turn, row)
        if rows[-1][1]:
            r["final"]["intent"] = rows[-1][1]


async def run_batch(
    cases: list,
    mode: str = "inproc",
    run_id: str = None,
    concurrency: int = 4,
    base_url: str = "http://127.0.0.1:8080",
    users: int = 1,
    base_user_id: int = 900001,
    jwt_secret: str = None,
    dsn: str = None,
    progress=None,
) -> list:
    """并发跑一批用例，返回结果列表（保持用例顺序）。

    http 模式用 users 个合成 uid 轮转分配用例：网关限流是按 uid 分片的
    （agent.qps=2 / concurrent=3），单 uid 开高并发会被 429 打回。
    """
    run_id = run_id or datetime.now().strftime("%m%d%H%M%S")
    sem = asyncio.Semaphore(max(1, concurrency))
    graph = None
    client = None
    tokens: dict = {}
    uid_by_case: dict = {}

    if mode == "inproc":
        from langgraph.checkpoint.memory import MemorySaver

        from agent.core.graph import build_graph
        graph = build_graph(MemorySaver())
    else:
        import httpx

        from . import sse

        users = max(1, int(users or 1))
        uids = [int(base_user_id) + i for i in range(users)]
        secret = sse.jwt_secret(jwt_secret)
        tokens = {uid: sse.sign_token(uid, secret) for uid in uids}
        uid_by_case = {c.id: uids[i % users] for i, c in enumerate(cases)}
        client = httpx.AsyncClient(
            timeout=sse.default_timeout(),
            limits=httpx.Limits(
                max_connections=max(8, concurrency * 2),
                max_keepalive_connections=max(4, concurrency),
            ),
            trust_env=False,  # 系统代理会拦 127.0.0.1；与 NO_PROXY=* 同口径
        )

    async def _one(case: Case):
        async with sem:
            if mode == "inproc":
                res = await run_case_inproc(case, graph, run_id)
            else:
                uid = uid_by_case[case.id]
                res = await run_case_http(
                    case, client, base_url, run_id, uid, tokens[uid],
                    _safe_session_id(run_id, case.id),
                )
            if progress:
                real_err = [e for e in res["errors"] if "guardrail_blocked" not in e]
                flag = "OK" if res["final"]["text"].strip() and not real_err else "BAD"
                progress(f"[{flag}] {case.id} {case.category}/{case.difficulty} "
                         f"{res['elapsed_ms_total'] / 1000:.1f}s cards={res['all_cards']}")
            return res

    if client is not None:
        async with client:
            results = list(await asyncio.gather(*[_one(c) for c in cases]))
    else:
        results = list(await asyncio.gather(*[_one(c) for c in cases]))

    if mode == "http":
        try:
            backfill_http(results, dsn or os.environ.get(
                "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/stock_agent"))
        except Exception as e:  # noqa: BLE001 - 审计回填失败不影响主结果
            print(f"[warn] agent_runs backfill failed: {e}")
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
