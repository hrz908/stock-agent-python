#!/usr/bin/env python
"""候选池 × 上线数量 扫描：collect → latency → replay → report。

为什么只打一遍接口就能扫全部组合：SEARCH_SQL 里 vec/lex 各 LIMIT 50 与外层
LIMIT %(limit)s 相互独立，RRF 分由窗口函数算出、与截断无关，所以 limit=N 的结果
恒等于 limit=50 结果的前 N 条。于是只需以 N=50 采集一次候选池，其余
(N, k) 组合全部在本地切片重放。重放顺序必须与线上一致：
切 RRF 前 N 条 → 按重排分排序 → 阈值裁剪（可选）→ _clean_docs → 切 k。

用法（Windows 控制台务必带 -X utf8）：
  python -X utf8 tests/benchmark/sweep_retrieval.py all     --tag sweep1
  python -X utf8 tests/benchmark/sweep_retrieval.py collect --tag sweep1
  python -X utf8 tests/benchmark/sweep_retrieval.py latency --tag sweep1 --latency-queries 20
  python -X utf8 tests/benchmark/sweep_retrieval.py replay  --tag sweep1

指标：Hit@k / Recall@N / MRR@k / NDCG@k / Noise@k / Coverage / 误升级率 / 负样本误命中。

产物：
  runs/<tag>/pool.jsonl    采集到的候选池（RRF 序，含正文，可反复重放）
  runs/<tag>/latency.json  各 N 的重排延迟分位
  runs/<tag>/sweep.json    完整矩阵
  runs/<tag>/report.md     报告与选优结论
"""
import argparse
import asyncio
import json
import math
import random
import sys
import time
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

HERE = Path(__file__).resolve().parent            # .../tests/benchmark
sys.path.insert(0, str(HERE.parents[2] / "src"))  # src/：gateway/agent/ingest 包根
sys.path.insert(0, str(HERE.parents[1]))          # tests/

RUNS_DIR = HERE / "runs"
DEFAULT_CASES = HERE / "retrieval_cases.jsonl"
POOL_N = 50          # 采集用的候选池上限，必须 >= 所有待测 N
PER_DOC = 3          # _clean_docs 默认值（RAG_PER_DOC），作为主口径
DEFAULT_NS = [10, 20, 30, 50]
DEFAULT_KS = [3, 4, 5, 6, 8]

# 判定规则（与方案文档一致）
RECALL_DROP = 0.02   # Recall@N 允许比 Recall@50 低 2pt
HIT_DROP = 0.01      # Hit@k 允许比最优 k 低 1pt
NOISE_MAX = 0.25     # Noise@k 上限
ESCALATE_SLACK = 0.01  # 误升级率允许比基线高 1pt
LAT_SLACK_MS = 300   # E2E p95 允许比基线高 300ms


# ===== 通用小工具 =====

def _setup_env() -> None:
    """加载 server/.env 并修正宿主直连项；必须在 import config/rag 之前调用。"""
    from benchmark.runner import apply_inproc_env
    apply_inproc_env(verbose=True)


_rag = None


def _rg():
    """惰性导入检索模块（config 在 import 时读环境变量，必须先 _setup_env）。"""
    global _rag
    if _rag is None:
        from agent.rag import retriever
        _rag = retriever
    return _rag


_ENCODER = None


def _tokens(text: str) -> int:
    global _ENCODER
    if _ENCODER is None:
        import tiktoken
        _ENCODER = tiktoken.get_encoding("cl100k_base")
    return len(_ENCODER.encode(text or ""))


def _pct(values: list, p: float):
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(p / 100 * (len(s) - 1)))))
    return s[idx]


def _mean(values: list):
    return sum(values) / len(values) if values else None


def _boot_ci(values: list, iters: int = 1000, seed: int = 42):
    """Bootstrap 95% 置信区间（均值），用于判断两组差异是否只是抽样噪声。"""
    n = len(values)
    if n < 2:
        return (None, None)
    rnd = random.Random(seed)
    means = []
    for _ in range(iters):
        means.append(sum(values[rnd.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return (means[int(0.025 * iters)], means[int(0.975 * iters) - 1])


def _fmt(v, pct: bool = False, digits: int = 3) -> str:
    if v is None:
        return "—"
    return f"{v:.1%}" if pct else f"{v:.{digits}f}"


def _path(tag: str, name: str) -> Path:
    return RUNS_DIR / tag / name


def load_cases(path) -> list:
    p = Path(path)
    if not p.exists():
        raise SystemExit(
            f"缺少评测集 {p}\n"
            f"  先生成：python -X utf8 tests/benchmark/gen_retrieval_cases.py --chunks 40\n"
            f"  格式参考：tests/benchmark/retrieval_cases.example.jsonl"
        )
    cases = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        c = json.loads(line)
        if c.get("sample") or not c.get("query"):
            continue
        cases.append(c)
    pos = [c for c in cases if not c.get("negative") and c.get("gold_chunk_ids")]
    neg = [c for c in cases if c.get("negative")]
    if not pos:
        raise SystemExit("评测集没有正样本（gold_chunk_ids 非空）")
    print(f"[cases] 正样本 {len(pos)} 条，负样本 {len(neg)} 条")
    return pos + neg


def load_pools(tag: str) -> list:
    p = _path(tag, "pool.jsonl")
    if not p.exists():
        raise SystemExit(f"缺少 {p}，先跑 collect")
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _dump(obj, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


# ===== collect：采集候选池 =====

def _pool_item(d) -> dict:
    return {
        "chunk_id": str(d.metadata.get("id", "")),
        "doc_id": str(d.metadata.get("doc_id", "")),
        "title": d.metadata.get("title", ""),
        "rrf": float(d.metadata.get("score", 0.0) or 0.0),
        "similarity": float(d.metadata.get("similarity", 0.0) or 0.0),
        "rerank_score": d.metadata.get("rerank_score"),
        "content": d.page_content or "",
    }


async def cmd_collect(args) -> list:
    _setup_env()
    rg = _rg()
    if not rg.RERANK_ENABLED:
        raise SystemExit("RERANK_ENABLED=false：本实验依赖重排分，先打开再跑")
    cases = load_cases(args.cases)
    if args.ns and max(args.ns) > args.pool:
        raise SystemExit(f"待测档位 N 最大 {max(args.ns)} 超过采集上限 --pool {args.pool}，调大 --pool")
    retriever = rg.build_retriever(k=args.pool, candidates=args.pool)
    print(f"[collect] 候选池上限 N={args.pool}（重排候选数=采集上限，见 RERANK_CANDIDATES）")

    sem = asyncio.Semaphore(max(1, args.concurrency))
    done = 0
    total = len(cases)

    async def one(c: dict) -> dict:
        nonlocal done
        async with sem:
            timings = {}
            query = c.get("rewritten") or c["query"]
            t0 = time.monotonic()
            failed = ""
            try:
                docs = await retriever._run_candidates(query, timings)
            except Exception as e:  # noqa: BLE001 - 单条失败记下来，不中断整批
                docs, failed = [], f"{type(e).__name__}: {e}"
            elapsed = int((time.monotonic() - t0) * 1000)
            done += 1
            print(f"[{done}/{total}] {c['qid']} pool={len(docs)} {elapsed}ms"
                  + (f" ERROR {failed}" if failed else ""), flush=True)
            return {
                "qid": c["qid"],
                "query": c["query"],
                "query_used": query,
                "gold": c.get("gold_chunk_ids") or [],
                "negative": bool(c.get("negative")),
                "difficulty": c.get("difficulty", ""),
                "elapsed_ms": elapsed,
                "timings": timings,
                "error": failed,
                "pool": [_pool_item(d) for d in docs],
            }

    records = await asyncio.gather(*[one(c) for c in cases])
    records = list(records)

    bad = [r for r in records if r["error"] or any(p["rerank_score"] is None for p in r["pool"])]
    if len(bad) > max(1, len(records) * 0.1):
        raise SystemExit(
            f"{len(bad)}/{len(records)} 条记录检索或重排失败（大概率是 RERANK_TIMEOUT 不够）。\n"
            f"  先调大 RERANK_TIMEOUT（N={args.pool} 时逐对推理更慢）再重跑；"
            f"失败样例：{bad[0]['qid']} {bad[0]['error'] or 'rerank_score=None'}"
        )

    out = _path(args.tag, "pool.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"DONE -> {out}")
    return records


# ===== latency：各 N 的重排延迟 =====

async def cmd_latency(args, pools: list = None) -> dict:
    _setup_env()
    from agent.rag.reranker import rerank_scores
    pools = pools or load_pools(args.tag)
    sample = [r for r in pools if r["pool"]][: args.latency_queries]
    if not sample:
        raise SystemExit("没有可用候选池，先跑 collect")

    # 预热：首次请求含模型冷启动，丢弃
    for _ in range(3):
        try:
            await rerank_scores("预热", [p["content"] for p in sample[0]["pool"][:5]])
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 预热失败: {e}")
            break

    out = {}
    for n in args.ns:
        lat, toks, errs = [], [], 0
        for rec in sample:
            texts = [p["content"] for p in rec["pool"][:n]]
            if not texts:
                continue
            toks.append(sum(_tokens(t) for t in texts))
            t0 = time.monotonic()
            try:
                await rerank_scores(rec["query_used"], texts)
            except Exception:  # noqa: BLE001
                errs += 1
                continue
            lat.append((time.monotonic() - t0) * 1000)
        out[str(n)] = {
            "samples": len(lat),
            "errors": errs,
            "p50_ms": round(_pct(lat, 50) or 0, 1),
            "p95_ms": round(_pct(lat, 95) or 0, 1),
            "tokens": round(_mean(toks) or 0, 1),
        }
        print(f"[latency] N={n:<3} p50={out[str(n)]['p50_ms']}ms "
              f"p95={out[str(n)]['p95_ms']}ms tokens={out[str(n)]['tokens']}")

    _dump(out, _path(args.tag, "latency.json"))
    print(f"DONE -> {_path(args.tag, 'latency.json')}")
    return out


# ===== replay：离线切片重放 =====

def _rank_metrics(ctx_ids: list, gold: set, k: int) -> tuple[float, float]:
    """二元相关的 MRR 与 NDCG@k；纯函数，便于脱离 langchain 单测。"""
    ranks = [i for i, cid in enumerate(ctx_ids) if cid in gold]
    mrr = 1.0 / (ranks[0] + 1) if ranks else 0.0
    dcg = sum(1.0 / math.log2(i + 2) for i in ranks)
    ideal = min(len(gold), max(k, 1))
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal))
    return mrr, (dcg / idcg if idcg else 0.0)


def _eval_one(rec: dict, n: int, k: int, filt: bool, per_doc: int,
              th: float, min_chunks: int, tok_cache: dict) -> dict:
    """按线上顺序复现一次检索：切 RRF 前 N → 重排排序 → 裁阈值 → clean → 切 k。"""
    from langchain_core.documents import Document
    rg = _rg()

    sub = rec["pool"][:n]
    docs = [Document(page_content=p["content"], metadata={
        "id": p["chunk_id"],
        "doc_id": p.get("doc_id", ""),
        "title": p["title"],
        "score": p["rrf"],
        "similarity": p["similarity"],
        "rerank_score": p["rerank_score"],
    }) for p in sub]
    docs.sort(key=lambda d: d.metadata.get("rerank_score") or 0.0, reverse=True)
    docs = rg._apply_rerank_filter(docs, th, enabled=filt)
    ctx = rg._clean_docs(docs, per_doc=per_doc)[:k]

    gold = set(rec["gold"])
    ctx_list = [d.metadata["id"] for d in ctx]
    ctx_ids = set(ctx_list)
    scores = [d.metadata.get("rerank_score") or 0.0 for d in ctx]
    top = max(scores) if scores else 0.0
    # 与 sufficiency_check 同口径：有效片段 <2 或最高分不过阈值即升级
    escalate = len(ctx) < min_chunks or top < th

    # 排序质量：Hit 只看有没有、MRR/NDCG 看排在第几；gold 是二元相关
    mrr, ndcg = _rank_metrics(ctx_list, gold, k)

    tokens = 0
    for d in ctx:
        cid = d.metadata["id"]
        if cid not in tok_cache:
            tok_cache[cid] = _tokens(d.page_content)
        tokens += tok_cache[cid]

    return {
        "hit": bool(gold & ctx_ids),
        "recall": bool(gold & {p["chunk_id"] for p in sub}),
        "mrr": mrr,
        "ndcg": ndcg,
        "n_ctx": len(ctx),
        "noise": sum(1 for s in scores if s < th) / k,
        "coverage": len({d.metadata["title"] for d in ctx}) / k,
        "tokens": tokens,
        "escalate": escalate,
    }


def _aggregate(rows: list) -> dict:
    hit = [1.0 if r["hit"] else 0.0 for r in rows]
    noise = [r["noise"] for r in rows]
    return {
        "hit": _mean(hit),
        "hit_ci": _boot_ci(hit),
        "recall": _mean([1.0 if r["recall"] else 0.0 for r in rows]),
        "mrr": _mean([r["mrr"] for r in rows]),
        "ndcg": _mean([r["ndcg"] for r in rows]),
        "noise": _mean(noise),
        "n_ctx": _mean([r["n_ctx"] for r in rows]),
        "coverage": _mean([r["coverage"] for r in rows]),
        "tokens": _mean([r["tokens"] for r in rows]),
        "escalate": _mean([1.0 if r["escalate"] else 0.0 for r in rows]),
    }


def cmd_replay(args) -> dict:
    _setup_env()
    from agent.config import RERANK_THRESHOLD
    pools = load_pools(args.tag)
    pos = [r for r in pools if not r["negative"] and r["gold"]]
    neg = [r for r in pools if r["negative"]]
    if not pos:
        raise SystemExit("候选池里没有正样本，检查评测集")

    pool_sizes = [len(r["pool"]) for r in pools]
    if pool_sizes and _mean(pool_sizes) < max(args.ns) * 0.9:
        print(f"[warn] 候选池均值只有 {_mean(pool_sizes):.0f} 条（最大档位 N={max(args.ns)}）："
              f"语料规模把 N 维度钳制住了，高位档位之间会等价")

    th = RERANK_THRESHOLD if args.threshold is None else args.threshold
    tok_cache: dict = {}
    matrix = []
    for filt in (False, True):
        for pd in args.per_docs:
            for n in args.ns:
                for k in args.ks:
                    rows = [_eval_one(r, n, k, filt, pd, th, args.min_chunks, tok_cache) for r in pos]
                    neg_rows = [_eval_one(r, n, k, filt, pd, th, args.min_chunks, tok_cache) for r in neg]
                    agg = _aggregate(rows)
                    neg_esc = [1.0 if r["escalate"] else 0.0 for r in neg_rows]
                    agg.update({
                        "n": n, "k": k, "filter": filt, "per_doc": pd,
                        "threshold": th,
                        # 负样本误命中 = 没升级（闸没拦住）
                        "false_hit": (1 - _mean(neg_esc)) if neg else None,
                        "false_hit_ci": _boot_ci([1 - v for v in neg_esc]) if neg else (None, None),
                    })
                    matrix.append(agg)
    print(f"[replay] {len(matrix)} 个组合 × {len(pos)} 正 / {len(neg)} 负")

    result = {
        "tag": args.tag,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "cases": {"positive": len(pos), "negative": len(neg)},
        "params": {"ns": args.ns, "ks": args.ks, "per_docs": args.per_docs,
                   "threshold": th, "min_chunks": args.min_chunks, "pool": POOL_N},
        "matrix": matrix,
    }
    _dump(result, _path(args.tag, "sweep.json"))
    print(f"DONE -> {_path(args.tag, 'sweep.json')}")
    return result


# ===== 选优 =====

def _decide(matrix: list, ns: list, ks: list, latency: dict = None) -> dict:
    """按「召回饱和 → 延迟预算 → 命中饱和 → 安全阀」选出 (N, k)，filter 各算一套。

    延迟预算用重排 p95 相对 N=20 的增量代理 E2E 增量：其余环节与 N 无关。
    """

    def lat_ok(n: int) -> bool:
        if not latency:
            return True
        cur = (latency.get(str(n)) or {}).get("p95_ms")
        ref = (latency.get(str(20)) or {}).get("p95_ms")
        if cur is None or ref is None:
            return True
        return (cur - ref) <= LAT_SLACK_MS
    base_key = (20, 6, False)
    base = next((r for r in matrix
                 if (r["n"], r["k"], r["filter"]) == base_key and r["per_doc"] == PER_DOC), None)
    n_max = max(ns)

    plans = []
    for filt in (False, True):
        rows = [r for r in matrix if r["filter"] == filt and r["per_doc"] == PER_DOC]
        by = {(r["n"], r["k"]): r for r in rows}
        rec_of = {r["n"]: r["recall"] for r in rows if r["k"] == ks[0]}
        rec_max = rec_of.get(n_max)
        if rec_max is None:
            continue
        target = rec_max - RECALL_DROP
        within = [n for n in sorted(ns) if rec_of[n] >= target]
        within_lat = [n for n in within if lat_ok(n)]
        lat_limited = False
        if within_lat:
            n_opt = within_lat[0]
        else:
            # 召回达标但全部超延迟预算：退到预算内召回最高的一档，并标注受限
            under = [n for n in sorted(ns) if lat_ok(n)]
            n_opt = under[-1] if under else sorted(ns)[0]
            lat_limited = True

        hits = {k: by[(n_opt, k)]["hit"] for k in ks if (n_opt, k) in by}
        hit_max = max(hits.values())
        esc_base = base["escalate"] if base else None
        k_opt, rejected = None, []
        for k in sorted(ks):
            r = by.get((n_opt, k))
            if not r:
                continue
            gate_hit = r["hit"] >= hit_max - HIT_DROP
            gate_noise = r["noise"] <= NOISE_MAX
            gate_esc = esc_base is None or r["escalate"] <= esc_base + ESCALATE_SLACK
            if gate_hit and gate_noise and gate_esc:
                k_opt = k
                break
            rejected.append({"k": k, "hit": gate_hit, "noise": gate_noise, "escalate": gate_esc})
        k_opt = k_opt or max(ks)
        best = by[(n_opt, k_opt)]

        lat = (latency or {}).get(str(n_opt), {})
        plans.append({
            "filter": filt,
            "n": n_opt,
            "k": k_opt,
            "recall": rec_of[n_opt],
            "recall_max": rec_max,
            "hit": best["hit"],
            "hit_max": hit_max,
            "mrr": best.get("mrr"),
            "ndcg": best.get("ndcg"),
            "noise": best["noise"],
            "escalate": best["escalate"],
            "esc_base": esc_base,
            "false_hit": best["false_hit"],
            "tokens": best["tokens"],
            "n_ctx": best["n_ctx"],
            "coverage": best["coverage"],
            "rerank_p95": lat.get("p95_ms"),
            "rerank_tokens": lat.get("tokens"),
            "lat_limited": lat_limited,
            "lat_ok": lat_ok(n_opt),
            "rejected": rejected,
        })
    # 方案裁决：命中最优 → 噪音最低 → 上下文最省
    overall = None
    if plans:
        overall = sorted(plans, key=lambda p: (-(p["hit"] or 0), p["noise"] or 1,
                                               p["tokens"] or 0))[0]
    return {
        "baseline": {"n": 20, "k": 6, "hit": base["hit"] if base else None,
                     "noise": base["noise"] if base else None,
                     "escalate": base["escalate"] if base else None,
                     "tokens": base["tokens"] if base else None},
        "plans": plans,
        "overall": overall,
        "rules": {"recall_drop": RECALL_DROP, "hit_drop": HIT_DROP, "noise_max": NOISE_MAX,
                  "escalate_slack": ESCALATE_SLACK, "lat_slack_ms": LAT_SLACK_MS},
    }


# ===== 报告 =====

def _table(header: list, rows: list) -> list:
    out = ["| " + " | ".join(header) + " |",
           "| " + " | ".join("---" for _ in header) + " |"]
    for r in rows:
        out.append("| " + " | ".join(r) + " |")
    return out


def render_report(sweep: dict, decision: dict, latency: dict) -> str:
    m = sweep["matrix"]
    ns, ks = sweep["params"]["ns"], sweep["params"]["ks"]
    th = sweep["params"]["threshold"]
    L = []
    add = L.append

    add(f"# 检索候选池 / 上线数量 扫描报告（{sweep['tag']}）\n")
    add(f"- 时间：{sweep['ts']}")
    add(f"- 评测集：正样本 {sweep['cases']['positive']} 条，负样本 {sweep['cases']['negative']} 条")
    add(f"- 采集候选池：N={sweep['params']['pool']}｜重排阈值：{th}｜per_doc 主口径：{PER_DOC}")
    add("")

    # ---------- 结论 ----------
    add("## 一、结论\n")
    ov = decision.get("overall")
    if ov:
        add(f"**总推荐：N={ov['n']}、k={ov['k']}，"
            f"{'开启' if ov['filter'] else '不开启'}重排阈值裁剪**"
            f"（Hit {_fmt(ov['hit'], True)}，Noise {_fmt(ov['noise'], True)}，"
            f"上下文 {_fmt(ov['tokens'], False, 0)} tokens）\n")
    for p in decision["plans"]:
        mode = "开启阈值裁剪（RAG_RERANK_FILTER=true）" if p["filter"] else "不裁剪上下文（默认行为）"
        add(f"### {'方案 B' if p['filter'] else '方案 A'}：{mode}\n")
        add(f"- **推荐 N={p['n']}，k={p['k']}**")
        if p["n"] == max(ns):
            add(f"- 召回：Recall@{p['n']} = {_fmt(p['recall'], True)}（已取到最大档位）")
        else:
            add(f"- 召回：Recall@{p['n']} = {_fmt(p['recall'], True)}"
                f"（Recall@{max(ns)} = {_fmt(p['recall_max'], True)}，保住 "
                f"{_fmt(p['recall'] / p['recall_max'], True) if p['recall_max'] else '—'}）")
        if p.get("lat_limited"):
            add(f"- ⚠ 延迟受限：召回未达饱和点，N 被延迟预算钳制"
                f"（重排 p95 相对 N=20 的增量上限 {LAT_SLACK_MS}ms）")
        add(f"- 命中：Hit@{p['k']} = {_fmt(p['hit'], True)}（该 N 下最优 {_fmt(p['hit_max'], True)}）"
            f"，MRR {_fmt(p.get('mrr'))}，NDCG {_fmt(p.get('ndcg'))}"
            f"，噪音率 {_fmt(p['noise'], True)}，进上下文均值 {_fmt(p['n_ctx'], False, 1)} 条")
        add(f"- 安全阀：误升级率 {_fmt(p['escalate'], True)}"
            f"（基线 {_fmt(p['esc_base'], True)}），负样本误命中 {_fmt(p['false_hit'], True)}")
        add(f"- 成本：上下文 {_fmt(p['tokens'], False, 0)} tokens"
            f"（基线 {_fmt(decision['baseline']['tokens'], False, 0)}），"
            f"重排 p95 {_fmt(p['rerank_p95'], False, 0)}ms")
        if p["rejected"]:
            r = p["rejected"][0]
            why = []
            if not r["hit"]:
                why.append("命中不足")
            if not r["noise"]:
                why.append("噪音超标")
            if not r["escalate"]:
                why.append("误升级超标")
            add(f"- 更小的 k={r['k']} 被否：{'/'.join(why)}")
        add("")

    add("**判定依据**：N 只由召回曲线拐点决定（Recall@N 不低于 Recall@50 − "
        f"{RECALL_DROP:.0%}），且重排 p95 相对 N=20 的增量不超过 {LAT_SLACK_MS}ms"
        "（其余环节与 N 无关，用重排增量代理 E2E 增量）；"
        f"k 只由命中饱和点决定（Hit@k 不低于最优 − {HIT_DROP:.0%}），"
        f"同时受噪音率 ≤{NOISE_MAX:.0%}、误升级率不高于基线的硬约束；两者都取满足条件的最小值。\n")

    if ov:
        add("落地配置：\n")
        add("```")
        add(f"RERANK_CANDIDATES={ov['n']}")
        add(f"RAG_TOP_K={ov['k']}")
        add(f"RAG_RERANK_FILTER={'true' if ov['filter'] else 'false'}")
        add("```")
        add("")

    # ---------- N 维度 ----------
    add("## 二、候选池 N 维度\n")
    k_ref = 6 if 6 in ks else ks[len(ks) // 2]
    add(f"固定 k={k_ref}、不裁剪上下文时，N 对召回与成本的影响：\n")
    rows = []
    for n in ns:
        r = next((x for x in m if x["n"] == n and x["k"] == k_ref
                  and not x["filter"] and x["per_doc"] == PER_DOC), None)
        if not r:
            continue
        lat = (latency or {}).get(str(n), {})
        rows.append([str(n), _fmt(r["recall"], True), _fmt(r["hit"], True),
                     _fmt(r.get("mrr")), _fmt(r.get("ndcg")), _fmt(r["noise"], True),
                     _fmt(r["tokens"], False, 0),
                     _fmt(lat.get("p50_ms"), False, 0), _fmt(lat.get("p95_ms"), False, 0),
                     _fmt(lat.get("tokens"), False, 0)])
    L += _table(["N", "Recall@N", "Hit@k", "MRR@k", "NDCG@k", "Noise@k", "上下文 tokens",
                 "重排 p50(ms)", "重排 p95(ms)", "重排 tokens"], rows)
    add("")

    # ---------- k 维度 ----------
    for idx, filt in enumerate((False, True)):
        p = next((x for x in decision["plans"] if x["filter"] == filt), None)
        if not p:
            continue
        num = ("三", "四")[idx]
        add(f"## {num}、上线数量 k 维度（N={p['n']}，"
            f"{'开启' if filt else '不'}阈值裁剪）\n")
        rows = []
        for k in ks:
            r = next((x for x in m if x["n"] == p["n"] and x["k"] == k and x["filter"] == filt
                      and x["per_doc"] == PER_DOC), None)
            if not r:
                continue
            mark = " ✅" if k == p["k"] else ""
            rows.append([f"{k}{mark}", _fmt(r["hit"], True),
                         f"{_fmt(r['hit_ci'][0], True)}~{_fmt(r['hit_ci'][1], True)}",
                         _fmt(r.get("mrr")), _fmt(r.get("ndcg")),
                         _fmt(r["noise"], True), _fmt(r["n_ctx"], False, 1),
                         _fmt(r["coverage"], True), _fmt(r["tokens"], False, 0),
                         _fmt(r["escalate"], True)])
        L += _table(["k", "Hit@k", "95%CI", "MRR@k", "NDCG@k", "Noise@k", "实进条数",
                     "Coverage", "tokens", "误升级率"], rows)
        add("")

    # ---------- 延迟 ----------
    if latency:
        add("## 五、延迟与资源\n")
        rows = []
        for n in ns:
            lat = latency.get(str(n))
            if not lat:
                continue
            rows.append([str(n), _fmt(lat.get("p50_ms"), False, 0), _fmt(lat.get("p95_ms"), False, 0),
                         _fmt(lat.get("tokens"), False, 0), str(lat.get("samples")),
                         str(lat.get("errors"))])
        L += _table(["N", "p50(ms)", "p95(ms)", "输入 tokens", "样本", "失败"], rows)
        add("")
        add("注：数据库侧开销与 N 无关（vec/lex 各 LIMIT 50 恒定），N 的成本只体现在重排耗时与上下文长度上。\n")

    # ---------- 敏感性 ----------
    pts = sorted({x["per_doc"] for x in m})
    if len(pts) > 1:
        add("## 六、per_doc 敏感性\n")
        rows = []
        for pt in pts:
            r = next((x for x in m if x["per_doc"] == pt and x["n"] == decision["plans"][0]["n"]
                      and x["k"] == decision["plans"][0]["k"] and not x["filter"]), None)
            if r:
                rows.append([str(pt), _fmt(r["hit"], True), _fmt(r["coverage"], True),
                             _fmt(r["noise"], True), _fmt(r["tokens"], False, 0)])
        L += _table(["per_doc", "Hit@k", "Coverage", "Noise@k", "tokens"], rows)
        add("")

    # ---------- 适用条件 ----------
    add("## 七、适用条件与失效情形\n")
    add("- 语料规模：若 chunks 总数远小于 50（融合后唯一候选取不到 N），N 之间等价，N 维免调；"
        "若超过 5 万，HNSW 默认 ef_search 会先成为召回瓶颈，需同步调 `SET hnsw.ef_search`。")
    add("- 单一文档密集：某主题只有 1–2 篇长文时 `per_doc` 是真实瓶颈，此时该调 per_doc 而不是 k。")
    add(f"- 阈值变动：RERANK_THRESHOLD 改动后 Noise@k 与「开启裁剪」方案的有效条数全变，结论需重跑"
        f"（当前口径 {th}）。")
    add("- 换重排模型：分数尺度不可跨模型比较，整份结论失效。")
    add("- 难题占比：hard 档占比升高时 k 的最优解右移（完整性收益盖过噪音代价）。")
    add("")
    return "\n".join(L)


def cmd_report(tag: str, latency: dict = None) -> Path:
    sweep = json.loads(_path(tag, "sweep.json").read_text(encoding="utf-8"))
    if latency is None:
        lp = _path(tag, "latency.json")
        latency = json.loads(lp.read_text(encoding="utf-8")) if lp.exists() else {}
    decision = _decide(sweep["matrix"], sweep["params"]["ns"], sweep["params"]["ks"], latency)
    _dump(decision, _path(tag, "decision.json"))
    md = render_report(sweep, decision, latency)
    out = _path(tag, "report.md")
    out.write_text(md, encoding="utf-8")
    print(f"DONE -> {out}")
    return out


# ===== CLI =====

def _ints(s: str) -> list:
    return [int(x) for x in s.split(",") if x.strip()]


def main():
    ap = argparse.ArgumentParser(description="检索候选池 × 上线数量 扫描")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--tag", required=True, help="运行标签（结果目录名）")
        p.add_argument("--cases", default=str(DEFAULT_CASES))
        p.add_argument("--ns", type=_ints, default=DEFAULT_NS, help="候选池档位，如 10,20,30,50")
        p.add_argument("--ks", type=_ints, default=DEFAULT_KS, help="上线数量档位，如 3,4,5,6,8")

    p_col = sub.add_parser("collect", help="采集候选池（唯一打接口的一步）")
    common(p_col)
    p_col.add_argument("--pool", type=int, default=POOL_N)
    p_col.add_argument("--concurrency", type=int, default=4)

    p_lat = sub.add_parser("latency", help="测各 N 的重排延迟")
    common(p_lat)
    p_lat.add_argument("--latency-queries", type=int, default=20)

    p_rep = sub.add_parser("replay", help="离线切片重放 + 选优")
    common(p_rep)
    p_rep.add_argument("--per-docs", type=_ints, default=[PER_DOC], help="per_doc 档位，如 2,3")
    p_rep.add_argument("--threshold", type=float, default=None, help="重排阈值，默认取 RERANK_THRESHOLD")
    p_rep.add_argument("--min-chunks", type=int, default=2, help="与 _MIN_CHUNKS 对齐")

    p_all = sub.add_parser("all", help="collect + latency + replay + report")
    common(p_all)
    p_all.add_argument("--pool", type=int, default=POOL_N)
    p_all.add_argument("--concurrency", type=int, default=4)
    p_all.add_argument("--latency-queries", type=int, default=20)
    p_all.add_argument("--per-docs", type=_ints, default=[PER_DOC, PER_DOC + 1])
    p_all.add_argument("--threshold", type=float, default=None)
    p_all.add_argument("--min-chunks", type=int, default=2)

    args = ap.parse_args()

    if args.cmd == "collect":
        asyncio.run(cmd_collect(args))
    elif args.cmd == "latency":
        asyncio.run(cmd_latency(args))
    elif args.cmd == "replay":
        cmd_replay(args)
        cmd_report(args.tag)
    elif args.cmd == "all":
        asyncio.run(cmd_collect(args))
        lat = asyncio.run(cmd_latency(args))
        cmd_replay(args)
        cmd_report(args.tag, lat)


if __name__ == "__main__":
    main()
