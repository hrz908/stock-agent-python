#!/usr/bin/env python
"""会话历史接口（/sessions*）契约测试 —— 「新对话」按钮的后端链路。

Go 网关的单测见 server/internal/agent/sessions_test.go，那里覆盖「鉴权 + 转发」；
这里覆盖 Agent 侧的入参校验、归属校验、返回结构，以及两条和「新对话」直接相关的口径：
  1. 占位注册（title 为空 → auto_title=True，标题先显示「新对话」，
     首条真实问题到达后才替换）；
  2. 恢复上下文只取 human / ai 纯文本（ToolMessage 与带 tool_calls 的 AIMessage
     对前端没有意义，还会破坏消息配对）。

DB / Redis / LangGraph 全部用假实现替掉：本套用例不打算起容器，
真实链路的验证走 tests/test_e2e.py。

用法：python -X utf8 tests/test_sessions_api.py
（Windows 控制台不加 -X utf8 会因 cp1252 编码在打印中文时抛异常）
"""
import asyncio
import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# TestClient 会把每次请求打成 INFO 日志，淹掉测试结果
logging.disable(logging.INFO)

from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

from app import main  # noqa: E402
from core import checkpointer  # noqa: E402
from core import sessions as sessions_mod  # noqa: E402

USER = "u_42"
SID = "sess_1761234567890_k3j2h1a9z"


# ===== 假的存储层 =====

class Recorder:
    """记录 Agent 侧收到的会话操作，替代真实 Postgres / checkpoint 读写。"""

    def __init__(self):
        self.upserts = []
        self.deleted = None

    async def upsert(self, user_id, session_id, title, auto_title=False):
        self.upserts.append(
            {"user_id": user_id, "session_id": session_id, "title": title, "auto_title": auto_title}
        )

    async def delete(self, user_id, session_id):
        self.deleted = (user_id, session_id)
        return True


class FakeCheckpointer:
    """假的 LangGraph saver：只提供 session_messages 需要的 aget_tuple。"""

    def __init__(self, messages):
        self._messages = messages

    async def aget_tuple(self, config):
        return SimpleNamespace(checkpoint={"channel_values": {"messages": self._messages}})


def build_client(recorder, messages=None):
    """构造一个注入了假存储的 TestClient。

    不用 with TestClient(app) 的形式：那会跑 lifespan（连 DB / Redis / 预热图），
    本套用例只想测接口本身。
    """
    async def fake_session_messages(user_id, session_id):
        return [
            {"role": "user", "content": "茅台现在多少钱"},
            {"role": "assistant", "content": "1680.50 元"},
        ]

    main.upsert_session = recorder.upsert
    main.list_sessions = lambda user_id, limit=20: asyncio.sleep(0, result=[
        {"session_id": SID, "title": "新对话", "updated_at": "2026-10-24T10:00:00+08:00"},
        {"session_id": "sess_old", "title": "茅台现在多少钱", "updated_at": "2026-10-23T09:00:00+08:00"},
    ])
    main.session_messages = fake_session_messages
    main.delete_session = recorder.delete
    return TestClient(main.app)


# ===== 用例 =====

def test_post_session_placeholder():
    """「新对话」按钮：title 为空 → 占位行（auto_title=True）。"""
    rec = Recorder()
    client = build_client(rec)
    r = client.post("/sessions", json={"user_id": USER, "session_id": SID, "title": ""})

    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}, r.text
    assert len(rec.upserts) == 1, rec.upserts
    got = rec.upserts[0]
    assert got["user_id"] == USER and got["session_id"] == SID
    assert got["title"] == ""
    assert got["auto_title"] is True, "空标题必须落占位（标题先显示「新对话」）"


def test_post_session_real_title():
    """每轮 run（Go 网关带 query 注册）：带标题时 auto_title=False，不让其覆盖已有标题。"""
    rec = Recorder()
    client = build_client(rec)
    r = client.post("/sessions", json={"user_id": USER, "session_id": SID, "title": "茅台现在多少钱"})

    assert r.status_code == 200, r.text
    assert rec.upserts[0]["title"] == "茅台现在多少钱"
    assert rec.upserts[0]["auto_title"] is False, "有真实标题时不应再标记占位"


def test_post_session_invalid_id():
    """非法 session_id 一律 400：它会拼进 thread_id 与 Redis key。"""
    rec = Recorder()
    client = build_client(rec)
    for bad in ["sess 1", "sess/1", "sess:1", "中文", "", "a" * 65]:
        r = client.post("/sessions", json={"user_id": USER, "session_id": bad, "title": ""})
        assert r.status_code == 400, (bad, r.status_code, r.text)
    assert rec.upserts == [], "非法 id 不应写库"


def test_post_session_missing_user():
    rec = Recorder()
    client = build_client(rec)
    r = client.post("/sessions", json={"user_id": "  ", "session_id": SID, "title": ""})
    assert r.status_code == 400, r.text
    assert rec.upserts == []


def test_get_sessions():
    client = build_client(Recorder())
    r = client.get("/sessions", params={"user_id": USER})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"sessions"}, body
    rows = body["sessions"]
    assert [x["session_id"] for x in rows] == [SID, "sess_old"], rows
    assert rows[0]["title"] == "新对话"
    # 三个字段与前端 services/sessions.js 的约定一一对应
    assert set(rows[0]) == {"session_id", "title", "updated_at"}, rows[0]


def test_get_sessions_bad_user():
    """user_id 缺失由 FastAPI 自己挡（422）；给了但为空由接口挡（400）。"""
    client = build_client(Recorder())
    assert client.get("/sessions").status_code == 422
    assert client.get("/sessions", params={"user_id": ""}).status_code == 400
    assert client.get("/sessions", params={"user_id": " "}).status_code == 400


def test_get_messages():
    client = build_client(Recorder())
    r = client.get(f"/sessions/{SID}/messages", params={"user_id": USER})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"messages"}, body
    assert body["messages"] == [
        {"role": "user", "content": "茅台现在多少钱"},
        {"role": "assistant", "content": "1680.50 元"},
    ]


def test_get_messages_invalid_id():
    client = build_client(Recorder())
    r = client.get("/sessions/sess%201/messages", params={"user_id": USER})
    assert r.status_code == 400, r.text


def test_delete_session():
    rec = Recorder()
    client = build_client(rec)
    r = client.delete(f"/sessions/{SID}", params={"user_id": USER})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "deleted": True}, r.text
    assert rec.deleted == (USER, SID)


def test_delete_session_invalid_id():
    rec = Recorder()
    client = build_client(rec)
    r = client.delete("/sessions/sess:1", params={"user_id": USER})
    assert r.status_code == 400, r.text
    assert rec.deleted is None, "非法 id 不应触发删除"


def test_thread_id_contract():
    """thread_id 口径必须与 Go 网关 sse_agent.go 一致。"""
    assert sessions_mod.thread_id_of(USER, SID) == f"default:{USER}:{SID}"
    # 冒号只允许出现在拼接里：session_id 自身含冒号会伪造出他人的 thread
    assert not sessions_mod.valid_session_id("a:b")


def test_session_messages_filters_non_text():
    """恢复上下文只留 human / ai 纯文本。

    ToolMessage 是工具返回的 JSON 载荷，带 tool_calls 的 AIMessage 是「正在调工具」的
    中间态，两者对前端都没意义，还会破坏消息配对（实测会把空气泡渲染进会话）。
    """
    msgs = [
        HumanMessage(content="茅台现在多少钱"),
        AIMessage(content="", tool_calls=[{"name": "get_quote", "args": {}, "id": "1"}]),
        ToolMessage(content='{"price": 1680.5}', tool_call_id="1"),
        AIMessage(content="1680.50 元"),
        AIMessage(content="   "),  # 空白内容也丢掉
    ]
    cp = FakeCheckpointer(msgs)

    async def fake_get_checkpointer():
        return cp

    origin = checkpointer.get_checkpointer
    checkpointer.get_checkpointer = fake_get_checkpointer
    try:
        out = asyncio.run(sessions_mod.session_messages(USER, SID))
    finally:
        checkpointer.get_checkpointer = origin

    assert out == [
        {"role": "user", "content": "茅台现在多少钱"},
        {"role": "assistant", "content": "1680.50 元"},
    ], out


def test_session_messages_empty_thread():
    """新对话没有 checkpoint：返回空列表，前端据此显示问候语（不是报错）。"""
    cp = FakeCheckpointer([])

    async def fake_get_checkpointer():
        return cp

    origin = checkpointer.get_checkpointer
    checkpointer.get_checkpointer = fake_get_checkpointer
    try:
        out = asyncio.run(sessions_mod.session_messages(USER, SID))
    finally:
        checkpointer.get_checkpointer = origin

    assert out == []


# ===== 运行 =====

def main_run():
    cases = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failed = []
    print("\n[新对话] Agent 会话接口测试")
    for name, fn in cases:
        try:
            fn()
            print("  PASS  " + name)
        except Exception as e:  # noqa: BLE001 - 测试跑完全部用例再汇总
            print("  FAIL  " + name)
            print("        " + repr(e))
            failed.append(name)
    print("\n  通过 %d/%d" % (len(cases) - len(failed), len(cases)))
    if failed:
        print("  失败：" + " | ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main_run()
