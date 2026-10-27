"""质量评估：规则断言 + LLM 评委五维打分 + 数字一致性诊断。

规则断言（确定性，不调 LLM）：
- 路由意图、工具调用、卡片发射、文案包含/排除、免责声明、非空、无 error

数字一致性（诊断项，不计入规则通过率）：
- 从卡片/工具数据里抽出真值，核对回答中出现的关键数字是否能在容差内对上；
- 只在有卡片或工具数据的轮次生效，概念/闲聊类自动跳过；
- 之所以不上门禁：答案可能做合理换算（万/亿、同比），误判成本高于收益。

LLM 评委（get_small_llm 结构化输出）五个维度各 1~5 分：
- accuracy       准确性：数字与事实是否与工具返回一致，有无编造
- completeness   完整性：是否覆盖问题各部分
- relevance      相关性：是否切题，有无无关内容
- timeliness     时效性：是否使用当期数据并说明数据日期
- clarity        表达清晰度：结构、简洁、易懂
另输出 issues（定位到的错误与不足）与 suggestion（改进建议）。

总分 = 规则通过率 * 50 + 评委均分 / 5 * 50（加权前报告里分开展示）。
"""
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

_JUDGE_SYSTEM = """你是一个严格的股票问答质量评审员。你将看到用户问题、助手回答、以及助手背后工具返回的真实数据（如有）。
请按五个维度各打 1~5 的整数分，并列出发现的问题与改进建议。

评分口径：
- accuracy 准确性：回答中的数字、事实、涨跌方向必须与工具数据一致；无工具数据时的通识回答不得出现编造的具体数字。5=完全一致，3=有小错误，1=编造关键数字或事实性错误。
- completeness 完整性：是否回答了问题的每一个部分（如问 PE 和 PB 两个都答了才算完整）。5=全覆盖，3=漏一部分，1=基本没答。
- relevance 相关性：是否紧扣问题，有无跑题、无关标的、冗余铺陈。5=完全切题，3=少量冗余，1=跑题。
- timeliness 时效性：是否使用当期数据并体现数据时间（日期/报告期）；是否把过期数据当实时数据用。5=数据新且口径清晰，3=未说明数据时间，1=使用或暗示过期数据。
- clarity 表达清晰度：结构是否清晰、表述是否易懂、有无照搬 JSON 或术语堆砌。5=清晰专业，3=基本可读，1=混乱难读。

注意：没有工具数据的概念/闲聊类回答，accuracy 只评通识正确性，不得因为「没有实时数据」而扣分。
issues 用中文简述具体问题（没有则空数组）；suggestion 用中文给一条最关键的改进建议。
只输出结构化结果，不要额外解释。"""


def rule_check(case, result: dict) -> list:
    """对单个用例结果跑规则断言，返回 [{check, pass, detail}]。"""
    expect = case.expect
    final = result.get("final", {})
    text = (final.get("text") or "").strip()
    low = text.lower()
    checks = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "pass": bool(ok), "detail": detail})

    add("回答非空", bool(text), f"len={len(text)}")
    # guardrail_blocked 是护栏的预期审计码（用户仍会收到安全降级话术），不算失败
    real_errors = [e for e in result.get("errors", []) if "guardrail_blocked" not in e]
    add("无错误事件", not real_errors, "; ".join(real_errors)[:200])

    if "intent" in expect:
        got = final.get("intent", "")
        add("路由意图正确", got in expect["intent"], f"got={got!r} want={sorted(expect['intent'])}")

    if "tools_any" in expect:
        got = set(result.get("all_tools") or [])
        hit = got & set(expect["tools_any"])
        add("调用了预期工具", bool(hit), f"got={sorted(got)} want_any={expect['tools_any']}")

    if "tools_all" in expect:
        got = set(result.get("all_tools") or [])
        missing = set(expect["tools_all"]) - got
        add("多跳链完整", not missing, f"missing={sorted(missing)}")

    if "cards_any" in expect:
        got = set(result.get("all_cards") or [])
        hit = got & set(expect["cards_any"])
        add("发射了预期卡片", bool(hit), f"got={sorted(got)} want_any={expect['cards_any']}")

    if expect.get("cards_none"):
        got = set(result.get("all_cards") or [])
        add("没有发射卡片", not got, f"got={sorted(got)}")

    # 产品规则：一次回复最多一张卡（多张会与正文并列抢视线）
    over = [t for t in (result.get("turns") or [])
            if len(t.get("cards") or []) + len(t.get("tables") or []) > 1]
    add("单轮最多一张卡片", not over,
        f"超限轮次={[{ 'cards': t.get('cards'), 'tables': t.get('tables') } for t in over]}")

    for sub in expect.get("must_contain", []):
        add(f"包含「{sub}」", sub.lower() in low, "")

    for sub in expect.get("must_not_contain", []):
        add(f"不含「{sub}」", sub.lower() not in low, "")

    # 产品规则（2026-10-26）：正文不写免责声明，前端也不再固定挂载声明条。
    # 这条对所有用例生效——模型最容易在这里回潮
    add("不含免责声明套话",
        not re.search(r"不构成.{0,8}投资建议|仅供参考|投资需谨慎|风险自负", text), "")

    return checks


_NUM_RE = re.compile(
    r"(?<![0-9A-Za-z.])(-?\d+(?:\.\d+)?)\s*(万亿|亿|万|%|％)?"
)
_YEAR_MIN, _YEAR_MAX = 1990, 2100
_UNIT_FACTOR = {"万亿": 1e12, "亿": 1e8, "万": 1e4}


def _to_number(match) -> tuple[float, float, str]:
    """返回 (换算成绝对值后的数值, 原始数值, 单位)。"""
    raw = float(match.group(1))
    unit = match.group(2) or ""
    return raw * _UNIT_FACTOR.get(unit, 1.0), raw, unit


def _collect_numbers(value, out: list, depth: int = 0) -> None:
    """把卡片 payload / 工具数据里的所有数字摊平进 out（绝对值）。"""
    if depth > 8 or len(out) >= 400:
        return
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        out.append(float(value))
        return
    if isinstance(value, str):
        for m in _NUM_RE.finditer(value):
            converted, _raw, _unit = _to_number(m)
            out.append(converted)
        return
    if isinstance(value, dict):
        for v in value.values():
            _collect_numbers(v, out, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value[:80]:
            _collect_numbers(v, out, depth + 1)


def _numeric_universe(result: dict) -> list:
    """真值来源：卡片 payload（http/inproc 都有）+ 工具 data_head（仅 inproc）。"""
    universe: list = []
    for turn in result.get("turns") or []:
        for item in turn.get("card_data") or []:
            _collect_numbers(item.get("payload"), universe)
        for tool in turn.get("tools") or []:
            _collect_numbers(tool.get("data_head"), universe)
    return universe


def _answer_numbers(text: str) -> list:
    out = []
    for m in _NUM_RE.finditer(text or ""):
        labeled, raw, unit = _to_number(m)
        if not unit:
            # 计数（3 只/2 倍）与年份不参与核对，避免整句都被判不一致
            if "." not in m.group(1) and (abs(labeled) < 100 or
                                          _YEAR_MIN <= labeled <= _YEAR_MAX):
                continue
        out.append({"text": m.group(0).strip(), "value": labeled,
                    "raw": raw, "unit": unit})
    return out


def _num_close(item: dict, universe: list) -> bool:
    """容差 1%（最少 0.02）；同时接受「1675.8亿」对上卡片里的 1675.8。"""
    cands = [item["value"], item["raw"]]
    unit = item["unit"]
    if unit in ("%", "％"):
        # 上游可能把 2.35% 存成 2.35，也可能存成 0.0235
        cands += [item["value"] / 100, item["value"] * 100]
    if unit in ("万", "亿", "万亿"):
        # 同一金额可能以元/万元/亿元三种口径落在卡片里
        cands += [item["value"] / 1e4, item["value"] / 1e8, item["value"] * 1e4]
    for target in cands:
        for truth in universe:
            # 用绝对值比较：正文写「跌 1.02%」而卡片存 -1.02，方向词不该算不一致
            if abs(abs(target) - abs(truth)) <= max(0.02, abs(truth) * 0.01):
                return True
    return False


def numeric_check(case, result: dict) -> dict:
    """回答数字 vs 卡片/工具真值。诊断用，不参与规则通过率。"""
    universe = _numeric_universe(result)
    text = (result.get("final") or {}).get("text") or ""
    items = _answer_numbers(text)
    if not universe or not items:
        return {"applicable": False, "checked": 0, "matched": 0, "rate": None,
                "unmatched": []}
    unmatched = [i["text"] for i in items if not _num_close(i, universe)]
    matched = len(items) - len(unmatched)
    return {
        "applicable": True,
        "checked": len(items),
        "matched": matched,
        "rate": round(matched / len(items), 3),
        "unmatched": unmatched[:10],
    }


_CITE_RE = re.compile(r"\[(\d{1,2})\]")


def citation_check(result: dict) -> dict:
    """RAG 引用落地：来源条数 vs 正文 [n] 标注。诊断用，不参与规则通过率。

    判定口径来自 agent/prompts/rag.xml：只允许引用 contexts 里的编号，
    所以正文出现 [n] 但 n 超过来源条数即为越界（编造来源）。
    有来源却一个 [n] 都没写，说明检索资料没被真正用上。
    """
    # 只看最后一轮带来源的轮次：多轮时 [n] 编号只对应当前轮的 contexts，
    # 把历史轮次的来源也拼进来会把合法编号误判成越界。
    docs = []
    for turn in reversed(result.get("turns") or []):
        if turn.get("source_docs"):
            docs = list(turn["source_docs"])
            break
    if not docs:
        return {"applicable": False, "sources": 0, "citations": 0, "valid": 0,
                "invalid": [], "cited": False, "coverage": None}
    text = (result.get("final") or {}).get("text") or ""
    markers = sorted({int(m) for m in _CITE_RE.findall(text)})
    valid = [m for m in markers if 1 <= m <= len(docs)]
    invalid = [m for m in markers if m > len(docs)]
    return {
        "applicable": True,
        "sources": len(docs),
        "citations": len(markers),
        "valid": len(valid),
        "invalid": invalid,
        "cited": bool(valid),
        "coverage": round(len(valid) / len(docs), 3),
    }


def _judge_model():
    from pydantic import BaseModel, Field

    class JudgeResult(BaseModel):
        accuracy: int = Field(description="准确性 1~5")
        completeness: int = Field(description="完整性 1~5")
        relevance: int = Field(description="相关性 1~5")
        timeliness: int = Field(description="时效性 1~5")
        clarity: int = Field(description="表达清晰度 1~5")
        issues: list = Field(default_factory=list, description="发现的问题，中文")
        suggestion: str = Field(default="", description="最关键的一条改进建议，中文")

    return JudgeResult


async def llm_judge(case, result: dict) -> dict:
    """LLM 评委：五维打分 + issues + suggestion。异常时返回全 None 的占位。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    from agent.core.llm import get_llm

    final = result.get("final", {})
    query = " -> ".join(t["query"] for t in result.get("turns", []))
    hint = getattr(case, "judge_hint", "") or ""
    tool_lines = []
    for t in final.get("tools", []):
        if t.get("ok"):
            tool_lines.append(f"[工具 {t['tool']} 返回] {t.get('data_head', '')}")
        else:
            tool_lines.append(f"[工具 {t['tool']} 失败] {t.get('error', '')}")
    tools_text = "\n".join(tool_lines)[:4000] or "（本轮无工具数据）"
    prompt = (
        f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"用户问题：\n{query}\n\n"
        f"助手回答：\n{final.get('text', '')}\n\n"
        f"工具返回的真实数据：\n{tools_text}\n\n"
        + (f"【预期行为说明】{hint}\n\n" if hint else "")
        + "请按五个维度打分并给出问题与建议。"
    )
    try:
        # 评委用主力模型：小模型（Qwen2.5-7B）对「婉拒/无数据即为正确行为」这类
        # 预期说明遵循度差，会把正确的拒绝打低分（实测 R-02/K-02）
        llm = get_llm(streaming=False, temperature=0.0)
        model = _judge_model()
        out = await llm.with_structured_output(model).ainvoke([
            SystemMessage(content=_JUDGE_SYSTEM),
            HumanMessage(content=prompt),
        ])
        return {
            "accuracy": out.accuracy, "completeness": out.completeness,
            "relevance": out.relevance, "timeliness": out.timeliness,
            "clarity": out.clarity,
            "issues": list(out.issues or []), "suggestion": out.suggestion or "",
        }
    except Exception as e:  # noqa: BLE001 - 评委故障不阻断评估流程
        return {"error": f"{type(e).__name__}: {e}",
                "accuracy": None, "completeness": None, "relevance": None,
                "timeliness": None, "clarity": None, "issues": [], "suggestion": ""}


async def evaluate(results: list, cases_by_id: dict, with_judge: bool = True,
                   concurrency: int = 4, progress=None, judge_sample: float = 1.0) -> list:
    """评估整批结果。with_judge=False 时只跑规则断言（离线快速模式）。

    judge_sample < 1 时按 id 排序做等距抽样，只对抽中的用例调评委：
    评委是主力模型、单条 3~5s，全量 120 条的评委成本比被测链路本身还高。
    """
    sem = asyncio.Semaphore(max(1, concurrency))
    evals = []
    judge_ids = None
    if with_judge and 0 < judge_sample < 1:
        ordered = sorted(results, key=lambda r: r["id"])
        step = max(1, round(1 / judge_sample))
        judge_ids = {r["id"] for r in ordered[::step]}
        print(f"[eval] 评委抽样 {len(judge_ids)}/{len(results)} 条（步骤 {step}）")

    async def _one(result: dict):
        case = cases_by_id.get(result["id"])
        if case is None:
            return None
        async with sem:
            checks = rule_check(case, result)
            use_judge = with_judge and (judge_ids is None or result["id"] in judge_ids)
            judge = await llm_judge(case, result) if use_judge else {"skipped": True}
            passed = sum(1 for c in checks if c["pass"])
            scores = [judge.get(k) for k in
                      ("accuracy", "completeness", "relevance", "timeliness", "clarity")]
            scores = [s for s in scores if isinstance(s, int)]
            rec = {
                "id": case.id, "category": case.category, "difficulty": case.difficulty,
                "rationale": case.rationale,
                "rule_total": len(checks), "rule_passed": passed,
                "rule_failures": [c for c in checks if not c["pass"]],
                "judge": judge,
                "judge_avg": round(sum(scores) / len(scores), 2) if scores else None,
                # 数字一致性与引用落地都是诊断项，不进 rule_pass_rate
                "numeric": numeric_check(case, result),
                "citation": citation_check(result),
            }
            if progress:
                j = rec["judge_avg"]
                progress(f"[eval] {case.id}: rules {passed}/{len(checks)}"
                         + (f" judge={j}" if j else ""))
            return rec

    for rec in await asyncio.gather(*[_one(r) for r in results]):
        if rec is not None:
            evals.append(rec)
    return evals


def summarize(evals: list) -> dict:
    """汇总：规则通过率、评委均分、按类型/难度、失败聚类、最差用例。"""
    def _avg(rows, key):
        vals = []
        for r in rows:
            v = r
            for part in key.split("."):
                v = v.get(part) if isinstance(v, dict) else None
                if v is None:
                    break
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vals.append(v)
        return round(sum(vals) / len(vals), 2) if vals else None

    total_rules = sum(r["rule_total"] for r in evals)
    passed_rules = sum(r["rule_passed"] for r in evals)
    numeric_rates = [
        r["numeric"]["rate"] for r in evals
        if (r.get("numeric") or {}).get("applicable")
    ]
    summary = {
        "cases": len(evals),
        "rule_pass_rate": round(passed_rules / total_rules, 3) if total_rules else None,
        "cases_all_rules_pass": sum(1 for r in evals if r["rule_passed"] == r["rule_total"]),
        "judge_avg": _avg(evals, "judge_avg"),
        "dims": {d: _avg(evals, f"judge.{d}") for d in
                 ("accuracy", "completeness", "relevance", "timeliness", "clarity")},
        "numeric_cases": len(numeric_rates),
        "numeric_rate": (round(sum(numeric_rates) / len(numeric_rates), 3)
                         if numeric_rates else None),
        "numeric_weak": [
            {"id": r["id"], "rate": r["numeric"]["rate"],
             "unmatched": r["numeric"]["unmatched"]}
            for r in evals
            if (r.get("numeric") or {}).get("applicable") and r["numeric"]["rate"] < 1.0
        ][:8],
        "citation_cases": 0,
        "citation_rate": None,
        "citation_invalid_cases": 0,
        "citation_missing": [],
        "by_category": {}, "by_difficulty": {},
    }
    cite_cases = [(r["id"], r["citation"]) for r in evals
                  if (r.get("citation") or {}).get("applicable")]
    if cite_cases:
        summary["citation_cases"] = len(cite_cases)
        summary["citation_rate"] = round(
            sum(1 for _, c in cite_cases if c["cited"]) / len(cite_cases), 3)
        summary["citation_invalid_cases"] = sum(1 for _, c in cite_cases if c["invalid"])
        summary["citation_missing"] = [cid for cid, c in cite_cases if not c["cited"]][:8]
    for dim in ("category", "difficulty"):
        groups = {}
        for r in evals:
            groups.setdefault(r[dim], []).append(r)
        summary["by_category" if dim == "category" else "by_difficulty"] = {
            k: {
                "cases": len(v),
                "rule_pass_rate": round(sum(x["rule_passed"] for x in v)
                                        / max(1, sum(x["rule_total"] for x in v)), 3),
                "judge_avg": _avg(v, "judge_avg"),
            } for k, v in sorted(groups.items())
        }
    # 失败聚类：按 check 名统计
    clusters = {}
    for r in evals:
        for f in r["rule_failures"]:
            clusters.setdefault(f["check"], []).append(r["id"])
    summary["failure_clusters"] = {k: v for k, v in
                                   sorted(clusters.items(), key=lambda kv: -len(kv[1]))}
    # 最差用例：先看规则失败数，再看评委分
    worst = sorted(evals, key=lambda r: (
        -(r["rule_total"] - r["rule_passed"]), r["judge_avg"] if r["judge_avg"] else 99))[:8]
    summary["worst"] = [{
        "id": r["id"], "category": r["category"],
        "rule_fail": r["rule_total"] - r["rule_passed"],
        "judge_avg": r["judge_avg"],
        "failures": [f["check"] for f in r["rule_failures"]],
        "issues": r["judge"].get("issues", [])[:3],
    } for r in worst]
    return summary


def save_evals(evals: list, path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(evals, ensure_ascii=False, indent=1), encoding="utf-8")


def load_evals(path) -> list:
    return json.loads(Path(path).read_text(encoding="utf-8"))
