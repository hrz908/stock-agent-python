#!/usr/bin/env python
"""AI 对话冒烟测试：向 agent /run/stream 逐条发送股票相关问题，
保存完整 ndjson 流到 agent_out_N.txt，并把可读摘要写到 agent_summary.md（UTF-8）。
控制台只打印 ASCII 状态，避免 Windows cp1252 报错。
注意：agent 在 localhost，禁用代理避免被本机 7897 代理拦截。"""
import json
import sys
import urllib.error
import urllib.request

URL = "http://localhost:8000/run/stream"

QUERIES = [
    ("Q1_quote", "查询贵州茅台（SH600519）的最新行情，包括最新价、涨跌幅、今开、最高、最低。"),
    ("Q2_analysis", "帮我分析一下宁德时代（SZ300750）的基本面投资价值，包括业绩和估值。"),
    ("Q3_metrics", "用通俗的语言解释市盈率(PE)、市净率(PB)和市销率(PS)的含义，以及怎么用它们给股票估值。"),
    ("Q4_valuation", "贵州茅台现在的估值水平如何？它当前的PE和PB大概是多少？"),
    ("Q5_fundflow", "最近比亚迪（SZ002594）的主力资金流向怎么样？是净流入还是净流出？"),
    ("Q6_report", "看一下宁德时代最新的财报主要财务指标。"),
]

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run_one(name, query, idx, md):
    payload = json.dumps({
        "thread_id": f"test-{idx}",
        "query": query,
        "user_id": "1",
    }).encode("utf-8")
    req = urllib.request.Request(URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    out_path = f"agent_out_{idx}.txt"
    text = []
    cards = []
    tables = []
    errors = []
    sources = []
    with open(out_path, "w", encoding="utf-8") as f:
        try:
            with opener.open(req, timeout=150) as resp:
                for raw in resp:
                    line = raw.decode("utf-8").strip()
                    if not line:
                        continue
                    f.write(line + "\n")
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    ev = obj.get("event")
                    data = obj.get("data", {})
                    if ev == "token":
                        text.append(data.get("delta", ""))
                    elif ev == "card":
                        cards.append(data)
                    elif ev == "table":
                        tables.append(data)
                    elif ev == "sources":
                        sources.append(data)
                    elif ev == "error":
                        errors.append(data.get("message", ""))
        except urllib.error.URLError as e:
            errors.append(f"URLError: {e}")
        except Exception as e:
            errors.append(f"Exception: {type(e).__name__}: {e}")

    full = "".join(text)
    md.write(f"## {name}\n\n")
    md.write(f"**问题**: {query}\n\n")
    md.write(f"**文本回答**:\n\n{full}\n\n")
    if cards:
        md.write("**卡片**: " + ", ".join(c.get("type", "?") for c in cards) + "\n\n")
    if tables:
        md.write("**表格**: " + ", ".join(t.get("type", "?") for t in tables) + "\n\n")
    if sources:
        md.write(f"**引用来源**: {len(sources)} 条\n\n")
    if errors:
        md.write("**错误**: " + " | ".join(errors) + "\n\n")
    md.write("\n")

    # 控制台只打 ASCII
    flag = "OK" if (full and not errors) else ("ERR" if errors else "EMPTY")
    print(f"[{idx}] {name}: {flag} cards={len(cards)} tables={len(tables)} errs={len(errors)} -> {out_path}")
    return {"name": name, "text": full, "cards": cards, "tables": tables, "errors": errors}


def main():
    report_path = sys.argv[1] if len(sys.argv) > 1 else 'e2e_report.md'
    with open(report_path, "w", encoding="utf-8") as md:
        md.write("# AI 对话功能测试结果\n\n")
        for i, (name, q) in enumerate(QUERIES, start=1):
            run_one(name, q, i, md)
    print("DONE ->", report_path)


if __name__ == "__main__":
    main()
