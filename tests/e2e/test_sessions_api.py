"""会话管理语义的回归测试（服务层，不依赖 DB / HTTP）。

历史背景：这段逻辑原先挂在已删除的独立 agent 服务（`app/main.py` 的 /sessions）上，
单进程化后 HTTP 入口变成 `gateway.api.v1.chat`，业务收敛到 `gateway.services.chat`。
测试随之改为直接驱动服务层：断言「网关传给 agent 的是什么」，比断言 HTTP 外壳更稳定。

用法：
    PYTHONPATH=src python tests/e2e/test_sessions_api.py
"""

from __future__ import annotations

import asyncio
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

# Windows 控制台默认可能是 cp1252，直接 print 中文会 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001, S110 - 老终端不支持时忽略
    pass

from gateway.adapters import agent_bridge  # noqa: E402
from gateway.infra.resp import ApiError  # noqa: E402
from gateway.services import chat as service  # noqa: E402

USER = 42
SID = "sess_1761234567890_k3j2h1a9z"
FAILED = 0


def check(name: str, cond: bool, detail: object = "") -> None:
    global FAILED
    if cond:
        print(f"  PASS {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}  {detail}")


class Recorder:
    """记录网关侧传给 agent 的会话操作。"""

    def __init__(self) -> None:
        self.upserts: list[dict] = []
        self.deleted: tuple | None = None

    async def upsert(self, user_id, session_id, title, auto_title=False):
        self.upserts.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                "title": title,
                "auto_title": auto_title,
            }
        )

    async def list(self, user_id):
        return [
            {"session_id": SID, "title": "新对话", "updated_at": "2026-01-01T00:00:00+08:00"},
            {"session_id": "sess_old", "title": "旧对话", "updated_at": "2025-12-31T00:00:00+08:00"},
        ]

    async def messages(self, user_id, session_id):
        return [
            {"role": "user", "content": "茅台现在多少钱"},
            {"role": "assistant", "content": "1680.50 元"},
        ]

    async def delete(self, user_id, session_id):
        self.deleted = (user_id, session_id)
        return True


def stub(rec: Recorder) -> tuple:
    originals = (
        agent_bridge.upsert_session,
        agent_bridge.list_sessions,
        agent_bridge.session_messages,
        agent_bridge.delete_session,
    )
    agent_bridge.upsert_session = rec.upsert
    agent_bridge.list_sessions = rec.list
    agent_bridge.session_messages = rec.messages
    agent_bridge.delete_session = rec.delete
    return originals


def restore(originals: tuple) -> None:
    (
        agent_bridge.upsert_session,
        agent_bridge.list_sessions,
        agent_bridge.session_messages,
        agent_bridge.delete_session,
    ) = originals


def test_session_id_rules() -> None:
    print("\n[1] session_id 白名单")
    for bad in ["sess 1", "sess/1", "sess:1", "中文", "", "a" * 65]:
        check(f"拒绝 {bad!r}", not service.valid_session_id(bad))
    check("接受正常 id", service.valid_session_id(SID))


def test_upsert_placeholder() -> None:
    print("\n[2] 新对话占位与标题")
    rec = Recorder()
    originals = stub(rec)
    try:
        # 「新对话」按钮：title 为空 → agent 侧落占位行（自动起标题）
        asyncio.run(service.create_session(USER, SID, ""))
        got = rec.upserts[0]
        check("空标题落占位", got["title"] == "")
        # auto_title 由 agent_bridge 决定，这一层只透传
        check("user_id 转字符串", got["user_id"] == str(USER), got["user_id"])

        asyncio.run(service.create_session(USER, SID, "茅台现在多少钱"))
        check("真实标题透传", rec.upserts[1]["title"] == "茅台现在多少钱")
    finally:
        restore(originals)


def test_list_and_messages() -> None:
    print("\n[3] 列表与消息")
    rec = Recorder()
    originals = stub(rec)
    try:
        rows = asyncio.run(service.list_sessions(USER))
        check("列表顺序保持", [x["session_id"] for x in rows] == [SID, "sess_old"], rows)
        # 三个字段与小程序 services/sessions.js 的约定一一对应
        check("字段与前端约定一致", set(rows[0]) == {"session_id", "title", "updated_at"}, rows[0])

        msgs = asyncio.run(service.session_messages(USER, SID))
        check(
            "消息结构",
            msgs
            == [
                {"role": "user", "content": "茅台现在多少钱"},
                {"role": "assistant", "content": "1680.50 元"},
            ],
            msgs,
        )
    finally:
        restore(originals)


def test_delete() -> None:
    print("\n[4] 删除会话")
    rec = Recorder()
    originals = stub(rec)
    try:
        deleted = asyncio.run(service.delete_session(USER, SID))
        check("返回 deleted", deleted is True)
        check("归属校验参数", rec.deleted == (str(USER), SID), rec.deleted)
    finally:
        restore(originals)


def test_agent_failure_maps_to_502() -> None:
    print("\n[5] agent 侧异常 → 502 业务码")

    async def boom(*args, **kwargs):
        raise RuntimeError("agent down")

    originals = (
        agent_bridge.upsert_session,
        agent_bridge.list_sessions,
        agent_bridge.session_messages,
        agent_bridge.delete_session,
    )
    agent_bridge.list_sessions = boom
    try:
        try:
            asyncio.run(service.list_sessions(USER))
            check("应抛 ApiError", False)
        except ApiError as exc:
            check("错误码 502", exc.code == 502, exc.code)
            check("文案面向用户", exc.message == "会话列表获取失败", exc.message)
    finally:
        restore(originals)


def main() -> int:
    test_session_id_rules()
    test_upsert_placeholder()
    test_list_and_messages()
    test_delete()
    test_agent_failure_maps_to_502()
    if FAILED:
        print(f"FAILED ({FAILED})")
        return 1
    print("ALL SESSION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
