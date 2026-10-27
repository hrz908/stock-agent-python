"""报告生成：单次测试报告 + A/B 对比报告（markdown）。"""
from datetime import datetime
from pathlib import Path

from .cases import CATEGORY_NAMES, DIFFICULTY_NAMES

DIM_NAMES = {
    "accuracy": "准确性", "completeness": "完整性", "relevance": "相关性",
    "timeliness": "时效性", "clarity": "表达清晰度",
}

STAGE_NAMES = {
    "guardrail_ms": "规则护栏",
    "rewrite_ms": "改写",
    "classify_ms": "意图分类",
    "retrieve_ms": "检索合计",
    "embed_ms": "Query 向量",
    "sql_ms": "混合检索 SQL",
    "rerank_ms": "重排",
    "sufficiency_ms": "命中判定",
    "judge_ms": "LLM 判读",
    "rag_ttft_ms": "RAG 首 token",
    "rag_generate_ms": "RAG 生成",
    "tool_call_ms": "工具决策 LLM",
    "tool_execute_ms": "工具执行",
    "synth_ttft_ms": "取数首 token",
    "synth_ms": "取数生成",
    "chat_ttft_ms": "闲聊首 token",
    "chat_generate_ms": "闲聊生成",
    "clarify_ms": "澄清",
    "refuse_ms": "婉拒",
    "post_ms": "收尾",
}


def _fmt(v, suffix=""):
    return "—" if v is None else f"{v}{suffix}"


def _pct(values: list, p: float):
    """线性插值分位数；无样本返回 None。"""
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(p / 100 * (len(s) - 1)))))
    return s[idx]


def _ms(value):
    return "—" if value is None else f"{int(round(value))}"


def _latency_row(name: str, turns: list, min_n: int = 10) -> list:
    """一行延迟分位；样本不足时在名称后打 *，避免把 3 条样本的 P99 当结论。"""
    ttft = [t["ttft_ms"] for t in turns if t.get("ttft_ms") is not None]
    first = [t["first_event_ms"] for t in turns if t.get("first_event_ms") is not None]
    total = [t["elapsed_ms"] for t in turns if t.get("elapsed_ms") is not None]
    mark = "" if len(turns) >= min_n else "*"
    return [
        f"{name}{mark}", str(len(turns)),
        _ms(_pct(ttft, 50)), _ms(_pct(ttft, 90)), _ms(_pct(ttft, 95)), _ms(_pct(ttft, 99)),
        _ms(_pct(first, 50)), _ms(_pct(first, 90)),
        _ms(_pct(total, 50)), _ms(_pct(total, 90)), _ms(_pct(total, 95)), _ms(_pct(total, 99)),
    ]


def render_report(results: list, evals: list, summary: dict, tag: str, mode: str) -> str:
    lines = []
    add = lines.append
    add(f"# 股票问答 Agent 测试报告（{tag}）\n")
    add(f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    add(f"- 模式：{mode}｜用例数：{summary['cases']}")
    total_s = sum(r.get("elapsed_ms_total", 0) for r in results) / 1000
    add(f"- 总耗时：{total_s:.0f}s\n")

    add("## 总览\n")
    add(f"- 规则通过率：**{_fmt(summary['rule_pass_rate'], '')}**"
        f"（{summary['cases_all_rules_pass']}/{summary['cases']} 用例全部通过）")
    add(f"- LLM 评委均分：**{_fmt(summary['judge_avg'])} / 5**")
    num_cases = summary.get("numeric_cases") or 0
    add(f"- 数字一致性（诊断，不计入门禁）：**{_fmt(summary.get('numeric_rate'))}**"
        f"（{num_cases} 条有卡片/工具数据）")
    cite_cases = summary.get("citation_cases") or 0
    if cite_cases:
        rate = summary.get("citation_rate")
        add(f"- 引用落地（诊断）：**{rate:.1%}**（{cite_cases} 条有资料来源，"
            f"越界引用 {summary.get('citation_invalid_cases') or 0} 条）\n")
    else:
        add("- 引用落地（诊断）：本批无资料来源，跳过\n")
    add("| 维度 | 均分 |")
    add("| --- | --- |")
    for k, name in DIM_NAMES.items():
        add(f"| {name} | {_fmt(summary['dims'].get(k))} |")
    add("")

    # ---------- 延迟 ----------
    turns_all = [(r, t) for r in results for t in (r.get("turns") or [])]
    add("## 延迟\n")
    add("| 范围 | 轮次 | TTFT P50(ms) | P90 | P95 | P99 | 首事件 P50 | P90 "
        "| 总耗时 P50 | P90 | P95 | P99 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    add("| " + " | ".join(_latency_row("全部", [t for _, t in turns_all])) + " |")
    by_cat_turns: dict = {}
    for r, t in turns_all:
        by_cat_turns.setdefault(r.get("category", "?"), []).append(t)
    for cat, ts in sorted(by_cat_turns.items()):
        add("| " + " | ".join(_latency_row(CATEGORY_NAMES.get(cat, cat), ts)) + " |")
    add("")
    add("注：答案 TTFT 取 token/card/table 的首个事件；首事件还包含 status / tool_start / "
        "sources 等过程提示。`*` 表示该行轮次 < 10，分位数仅供参考，不作验收依据。"
        "旧结果没有 first_event_ms / 阶段计时时显示 —。\n")

    stage_values: dict = {}
    for _, t in turns_all:
        for k, v in (t.get("timings") or {}).items():
            if isinstance(v, (int, float)):
                stage_values.setdefault(k, []).append(v)
    if stage_values:
        add("### 阶段耗时\n")
        add("| 阶段 | 样本 | P50(ms) | P90(ms) | P95(ms) |")
        add("| --- | --- | --- | --- | --- |")
        ordered = [k for k in STAGE_NAMES if k in stage_values]
        ordered += [k for k in stage_values if k not in STAGE_NAMES]
        for k in ordered:
            vals = stage_values[k]
            add(f"| {STAGE_NAMES.get(k, k)} | {len(vals)} | "
                f"{_ms(_pct(vals, 50))} | {_ms(_pct(vals, 90))} | {_ms(_pct(vals, 95))} |")
        add("")
        add("注：http 模式的阶段耗时来自 `agent_runs.timings`（服务端真值）；"
            "`rerank_failed` 是计数不是毫秒，出现即说明该轮重排被静默降级为 RRF。\n")

    add("## 分类表现\n")
    add("| 类型 | 用例数 | 规则通过率 | 评委均分 |")
    add("| --- | --- | --- | --- |")
    for k, v in summary["by_category"].items():
        add(f"| {CATEGORY_NAMES.get(k, k)} | {v['cases']} | {_fmt(v['rule_pass_rate'])} "
            f"| {_fmt(v['judge_avg'])} |")
    add("")
    add("| 难度 | 用例数 | 规则通过率 | 评委均分 |")
    add("| --- | --- | --- | --- |")
    for k, v in summary["by_difficulty"].items():
        add(f"| {DIFFICULTY_NAMES.get(k, k)} | {v['cases']} | {_fmt(v['rule_pass_rate'])} "
            f"| {_fmt(v['judge_avg'])} |")
    add("")

    if summary.get("failure_clusters"):
        add("## 失败聚类（按规则断言）\n")
        for check, ids in summary["failure_clusters"].items():
            add(f"- **{check}** × {len(ids)}：{', '.join(ids)}")
        add("")

    if summary.get("worst"):
        add("## 重点问题用例\n")
        for w in summary["worst"]:
            if not w["failures"] and (w["judge_avg"] or 5) >= 4:
                continue
            add(f"### {w['id']}（{CATEGORY_NAMES.get(w['category'], w['category'])}）"
                f"规则失败 {w['rule_fail']} 项，评委 {_fmt(w['judge_avg'])}")
            if w["failures"]:
                add(f"- 规则失败：{', '.join(w['failures'])}")
            for issue in w["issues"]:
                add(f"- 问题：{issue}")
            add("")

    add("## 逐用例明细\n")
    by_id = {r["id"]: r for r in results}
    for ev in evals:
        r = by_id.get(ev["id"], {})
        turns = r.get("turns", [])
        add(f"### {ev['id']} [{CATEGORY_NAMES.get(ev['category'], ev['category'])}/"
            f"{DIFFICULTY_NAMES.get(ev['difficulty'], ev['difficulty'])}]"
            f" 规则 {ev['rule_passed']}/{ev['rule_total']}，评委 {_fmt(ev['judge_avg'])}\n")
        add(f"- 设计意图：{ev['rationale']}")
        add(f"- 路由：{r.get('final', {}).get('intent', '?')}｜工具："
            f"{', '.join(r.get('all_tools', [])) or '无'}｜卡片："
            f"{', '.join(r.get('all_cards', [])) or '无'}")
        for f in ev["rule_failures"]:
            add(f"- ❌ {f['check']}：{f['detail']}")
        j = ev["judge"]
        if isinstance(j, dict) and j.get("accuracy") is not None:
            add(f"- 评委：准确 {j['accuracy']}｜完整 {j['completeness']}｜相关 "
                f"{j['relevance']}｜时效 {j['timeliness']}｜清晰 {j['clarity']}")
        num = ev.get("numeric") or {}
        if num.get("applicable"):
            line = f"- 数字核对：{num['matched']}/{num['checked']} 命中"
            if num.get("unmatched"):
                line += f"，未匹配：{', '.join(num['unmatched'])}"
            add(line)
        cite = ev.get("citation") or {}
        if cite.get("applicable"):
            if cite["cited"]:
                line = f"- 引用：{cite['valid']}/{cite['sources']} 条来源被 [n] 标注"
            else:
                line = f"- 引用：**未标注 [n]**（来源 {cite['sources']} 条）"
            if cite["invalid"]:
                line += f"，越界编号 {cite['invalid']}"
            add(line)
        for issue in (j.get("issues") or [])[:5]:
            add(f"- 问题：{issue}")
        if j.get("suggestion"):
            add(f"- 建议：{j['suggestion']}")
        if turns:
            text = (turns[-1].get("text") or "").strip()
            preview = text if len(text) <= 600 else text[:600] + " …"
            add(f"\n<details><summary>最终回答（{len(text)} 字）</summary>\n\n{preview}\n\n</details>\n")
    return "\n".join(lines)


def render_compare(before: dict, after: dict, tag_b: str, tag_a: str,
                   evals_b: list, evals_a: list) -> str:
    """A/B 对比：总体指标 + 分维度 + 分类型 + 逐用例变化。"""
    lines = []
    add = lines.append
    add(f"# A/B 对比：{tag_b} → {tag_a}\n")
    add("| 指标 | 前 | 后 | 变化 |")
    add("| --- | --- | --- | --- |")

    def row(name, b, a, higher_better=True, pct=False):
        if b is None or a is None:
            add(f"| {name} | {_fmt(b)} | {_fmt(a)} | — |")
            return
        delta = a - b
        sign = "+" if delta > 0 else ""
        good = (delta > 0) if higher_better else (delta < 0)
        mark = " ✅" if abs(delta) > 1e-9 and good else (" ⚠️" if abs(delta) > 1e-9 else "")
        fmt = (lambda x: f"{x:.1%}") if pct else (lambda x: f"{x:.2f}")
        add(f"| {name} | {fmt(b)} | {fmt(a)} | {sign}{fmt(delta).lstrip('+')}{mark} |")

    row("规则通过率", before["rule_pass_rate"], after["rule_pass_rate"], pct=True)
    row("全部通过的用例数", before["cases_all_rules_pass"], after["cases_all_rules_pass"])
    row("评委均分", before["judge_avg"], after["judge_avg"])
    row("数字一致性", before.get("numeric_rate"), after.get("numeric_rate"), pct=True)
    row("引用落地率", before.get("citation_rate"), after.get("citation_rate"), pct=True)
    for k, name in DIM_NAMES.items():
        row(name, before["dims"].get(k), after["dims"].get(k))
    add("")

    add("## 分类型对比\n")
    add("| 类型 | 规则通过率 前→后 | 评委均分 前→后 |")
    add("| --- | --- | --- |")
    cats = sorted(set(before["by_category"]) | set(after["by_category"]))
    for c in cats:
        b = before["by_category"].get(c, {})
        a = after["by_category"].get(c, {})
        add(f"| {CATEGORY_NAMES.get(c, c)} | {_fmt(b.get('rule_pass_rate'))} → "
            f"{_fmt(a.get('rule_pass_rate'))} | {_fmt(b.get('judge_avg'))} → "
            f"{_fmt(a.get('judge_avg'))} |")
    add("")

    # 逐用例：规则失败数 + 评委分变化
    b_by = {e["id"]: e for e in evals_b}
    a_by = {e["id"]: e for e in evals_a}
    add("## 逐用例变化\n")
    add("| 用例 | 规则失败 前→后 | 评委分 前→后 | 备注 |")
    add("| --- | --- | --- | --- |")
    for cid in sorted(set(b_by) | set(a_by)):
        b, a = b_by.get(cid), a_by.get(cid)
        if not b or not a:
            add(f"| {cid} | — | — | 仅单侧存在 |")
            continue
        bf = b["rule_total"] - b["rule_passed"]
        af = a["rule_total"] - a["rule_passed"]
        note = ""
        if af < bf:
            note = "修复"
        elif af > bf:
            note = "**回退**"
        elif (a["judge_avg"] or 0) > (b["judge_avg"] or 0) + 0.3:
            note = "提升"
        elif (a["judge_avg"] or 0) < (b["judge_avg"] or 0) - 0.3:
            note = "**下降**"
        add(f"| {cid} | {bf} → {af} | {_fmt(b['judge_avg'])} → {_fmt(a['judge_avg'])} | {note} |")
    add("")
    return "\n".join(lines)


def save_report(md: str, path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(md, encoding="utf-8")
