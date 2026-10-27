#!/usr/bin/env python
"""「新对话」按钮的端到端联调脚本（真实链路，不加假实现）。

链路：小程序按钮 → POST /api/v1/agent/sessions → Go 网关（鉴权 + 转发）
      → Python Agent /sessions → Postgres chat_sessions（+ LangGraph checkpoint）

与另两套测试的分工：
  stock-agent-miniapp/tests/newchat/          前端交互（假 wx / 假服务）
  server/internal/agent/sessions_test.go      网关转发（假 Agent）
  tests/e2e/test_sessions_api.py            Agent 契约（假存储）
  本脚本                                      三家真身串起来，只跑真实进程

前置：postgres / redis 已起（deploy/compose/dev.yml），服务用 bash scripts/dev.sh dev 起，
      Go 网关已在 8080 监听。
用法：python -X utf8 tests/e2e/new_chat.py [--base http://localhost:8080]
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

PASS, FAIL = [], []


def call(base, method, path, token=None, body=None):
    """发一次请求，返回 (http_status, body_dict)；网络层异常直接抛。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw or "{}")
        except ValueError:
            return e.code, {"raw": raw}


def data_of(body):
    """统一响应体的 data 字段；失败响应里它是 null，取成 {} 方便后续 .get。"""
    return body.get("data") or {}


def sse_events(base, token, query, sid, timeout=180):
    """发一次真实提问并逐帧收 SSE 事件（生成器）。

    只在这一档用得到：验证「新对话 → 发第一条消息」这条完整链路跑通后，
    会话标题会被首个问题替换、上下文也能原样恢复。会真的调 LLM，默认不跑。
    """
    req = urllib.request.Request(base + "/api/v1/agent/stream",
                                 data=json.dumps({"query": query, "session_id": sid}).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "text/event-stream")

    with urllib.request.urlopen(req, timeout=timeout) as r:
        ev = data = None
        for raw in r:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
            elif not line and ev:
                yield ev, data
                ev = data = None


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  PASS  " + name)
    else:
        FAIL.append(name)
        print("  FAIL  " + name + ("\n        " + str(detail) if detail else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    # 真的发一条消息走 SSE + LLM：慢且消耗 token，默认关
    ap.add_argument("--with-stream", action="store_true")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print("\n[新对话] 端到端联调  base=" + base)

    # ---- 登录：未配微信凭据时退化为固定演示账号（openid=demo）----
    st, body = call(base, "POST", "/api/v1/auth/login", body={"code": "demo"})
    ok = st == 200 and body.get("code") == 0 and data_of(body).get("token")
    check("S1 登录拿 token（code=demo）", ok, body)
    if not ok:
        print(f"\n  通过 {len(PASS)}/{len(PASS) + len(FAIL)}")
        sys.exit(1)
    token = body["data"]["token"]
    uid = body["data"]["user_id"]

    # 用例之间用不同 sid，脚本可重复跑而不互相干扰
    sid = f"sess_e2e_{int(time.time())}"

    # ---- 正常：注册新对话 ----
    st, body = call(base, "POST", "/api/v1/agent/sessions", token,
                    {"session_id": sid, "title": ""})
    check("S2 注册新对话 → 200/code=0", st == 200 and body.get("code") == 0, (st, body))
    check("S2 data.session_id 原样回传", data_of(body).get("session_id") == sid, body)

    # ---- 正常：立刻出现在历史列表（标题占位「新对话」）----
    st, body = call(base, "GET", "/api/v1/agent/sessions", token)
    rows = data_of(body).get("sessions") or []
    mine = [r for r in rows if r.get("session_id") == sid]
    check("S3 新对话立即出现在历史列表", st == 200 and len(mine) == 1, (st, rows[:3]))
    check("S3 占位标题为「新对话」", bool(mine) and mine[0].get("title") == "新对话", mine)
    check("S3 列表按最近活跃倒序（新会话在首位）", bool(rows) and rows[0].get("session_id") == sid, rows[:2])

    # ---- 正常：新会话恢复为空，前端据此显示问候语 ----
    st, body = call(base, "GET", f"/api/v1/agent/sessions/{sid}/messages", token)
    check("S4 恢复空会话 → messages=[]", st == 200 and data_of(body).get("messages") == [],
          (st, body))

    # ---- 正常：首条真实提问替换占位标题 ----
    st, body = call(base, "POST", "/api/v1/agent/sessions", token,
                    {"session_id": sid, "title": "茅台现在多少钱"})
    check("S5 首条提问 upsert 成功", st == 200 and body.get("code") == 0, (st, body))
    st, body = call(base, "GET", "/api/v1/agent/sessions", token)
    rows = data_of(body).get("sessions") or []
    mine = [r for r in rows if r.get("session_id") == sid]
    check("S5 标题被首个问题替换", bool(mine) and mine[0].get("title") == "茅台现在多少钱", mine)

    # ---- 异常：非法 session_id（会拼进 thread_id 与 Redis key）----
    for bad, why in [("sess 1", "含空格"), ("sess/1", "含斜杠"), ("sess:1", "含冒号可伪造 thread"),
                     ("", "空")]:
        st, body = call(base, "POST", "/api/v1/agent/sessions", token,
                        {"session_id": bad, "title": ""})
        check(f"S6 非法 session_id（{why}）→ 400",
              st == 400 and body.get("code") == 400, (st, body))
    st, body = call(base, "GET", "/api/v1/agent/sessions/bad%20id/messages", token)
    check("S6 非法 session_id 查询消息 → 400", st == 400 and body.get("code") == 400, (st, body))

    # ---- 异常：未带 token ----
    st, body = call(base, "GET", "/api/v1/agent/sessions")
    check("S7 未带 token → 401", st == 401, (st, body))

    # ---- 可选：真实提问（SSE + LLM），走完「新对话 → 发第一条消息」的完整生命周期 ----
    if args.with_stream:
        sid2 = sid + "_s"
        query = "你好，用一句话介绍你自己"
        st, body = call(base, "POST", "/api/v1/agent/sessions", token,
                        {"session_id": sid2, "title": ""})
        check("S9 注册新对话（流式档）", st == 200 and body.get("code") == 0, (st, body))

        kinds = []
        try:
            for ev, _ in sse_events(base, token, query, sid2):
                kinds.append(ev)
                if ev in ("done", "error"):
                    break
        except Exception as e:  # noqa: BLE001 - 上游不可用时也要把后面的断言跑完
            check("S9 SSE 链路可用", False, repr(e))
        else:
            check("S9 流式问答走到 done（无 error 帧）",
                  "done" in kinds and "error" not in kinds, kinds)

        st, body = call(base, "GET", "/api/v1/agent/sessions", token)
        rows = data_of(body).get("sessions") or []
        mine = [r for r in rows if r.get("session_id") == sid2]
        check("S9 标题被首个真实问题替换",
              bool(mine) and mine[0].get("title") == query, mine)

        st, body = call(base, "GET", f"/api/v1/agent/sessions/{sid2}/messages", token)
        msgs = data_of(body).get("messages") or []
        check("S9 上下文可恢复（user 提问 + assistant 回答）",
              len(msgs) == 2 and msgs[0].get("role") == "user"
              and msgs[0].get("content") == query and msgs[1].get("role") == "assistant", msgs)

        call(base, "DELETE", f"/api/v1/agent/sessions/{sid2}", token)

    # ---- 删除：一次真删、一次幂等 ----
    st, body = call(base, "DELETE", f"/api/v1/agent/sessions/{sid}", token)
    check("S8 删除会话 → 200/deleted=true",
          st == 200 and data_of(body).get("deleted") is True, (st, body))
    st, body = call(base, "DELETE", f"/api/v1/agent/sessions/{sid}", token)
    check("S8 重复删除幂等 → deleted=false",
          st == 200 and data_of(body).get("deleted") is False, (st, body))
    st, body = call(base, "GET", "/api/v1/agent/sessions", token)
    rows = data_of(body).get("sessions") or []
    check("S8 删除后不再出现在列表", all(r.get("session_id") != sid for r in rows), rows[:3])

    print(f"\n  用户 user_id={uid}；通过 {len(PASS)}/{len(PASS) + len(FAIL)}")
    if FAIL:
        print("  失败：" + " | ".join(FAIL))
        sys.exit(1)


if __name__ == "__main__":
    main()
