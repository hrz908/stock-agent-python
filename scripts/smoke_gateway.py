"""网关契约冒烟（不依赖真实 PostgreSQL / Redis）。

用 ASGITransport 直连 app、fakeredis 真实执行限流 Lua 脚本、假 PG 连接池，
验证的是「对外契约」而不是「第三方连通性」：

  1. 统一信封 {code,message,data} 与 healthz
  2. 鉴权：无 token / 坏 token 一律 401 + 固定文案
  3. 限流：滑动窗口 Lua 生效，超限回 429（业务码同为 429）
  4. 对话：SSE 帧格式、事件名、done 收尾
  5. 工具：金额定点格式化、股票代码归一化（这两处错了前端全废）

用法：
    .venv/Scripts/python.exe scripts/smoke_gateway.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认可能是 cp1252，直接 print 中文会 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 - 老终端不支持时忽略
    pass

import fakeredis.aioredis as fake_aioredis  # noqa: E402
import httpx  # noqa: E402

from server import agent_bridge  # noqa: E402
from server.agent_limiter import AgentLimiter  # noqa: E402
from server.config import Config  # noqa: E402
from server.deps import Runtime, set_runtime  # noqa: E402
from server.domain.stockcode import display_code, normalize_code  # noqa: E402
from server.main import create_app  # noqa: E402
from server.middleware.rate_limit import SlidingWindowLimiter  # noqa: E402
from server.security.jwt import sign_token  # noqa: E402
from server.util.amount import money, thousands, with_sign  # noqa: E402

JWT_SECRET = "smoke-secret"
USER_ID = 7

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(name)


# ---------------------------------------------------------------- 假 PG


class _Cursor:
    def __init__(self, rows: list, rowcount: int = 0) -> None:
        self._rows = rows
        self.rowcount = rowcount

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return list(self._rows)


class _NullTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def transaction(self):
        return _NullTxn()

    async def execute(self, sql: str, params=None):
        text = " ".join(sql.split()).lower()
        if "select count(*)" in text:
            return _Cursor([(0,)])
        if "from user_watchlist" in text:
            return _Cursor([])
        if text.startswith("select id, openid, nickname, avatar from users"):
            return _Cursor([(USER_ID, "demo", "微信用户", "")])
        if text.startswith("select nickname, avatar from users"):
            return _Cursor([("微信用户", "")])
        if "insert into users" in text:
            return _Cursor([(USER_ID, "demo", "微信用户", "")])
        return _Cursor([], rowcount=1)


class _Pool:
    """Stand-in for server.db.postgres.Postgres：业务代码统一走 `pg.pool.connection()`。"""

    @property
    def pool(self):
        return self

    def connection(self):
        return _Conn()


def build_app(cfg: Config, redis) -> tuple:
    app = create_app(cfg)
    rt = Runtime(
        cfg=cfg, pg=_Pool(), redis=redis, limiter=SlidingWindowLimiter(redis)  # type: ignore[arg-type]
    )
    rt.services["agent_limiter"] = AgentLimiter(
        redis, cfg.agent.qps, cfg.agent.concurrent, cfg.agent.daily_quota
    )
    rt.services["run_semaphore"] = asyncio.Semaphore(cfg.agent.proxy_concurrency)
    set_runtime(rt)
    app.state.runtime = rt
    return app, rt


def make_config(**overrides) -> Config:
    cfg = Config(jwt_secret=JWT_SECRET, env="dev")
    cfg.upload_dir = tempfile.mkdtemp(prefix="smoke-uploads-")
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def auth_headers() -> dict[str, str]:
    token = sign_token(USER_ID, JWT_SECRET, 3600)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- 假 agent 事件流


async def _fake_stream(thread_id: str, query: str, user_id: str):
    yield {"event": "status", "data": {"stage": "thinking", "label": "正在思考…"}}
    yield {"event": "token", "data": {"delta": "你好"}}
    yield {"event": "card", "data": {"type": "quote_card", "payload": {"code": "600519"}}}
    yield {"event": "sources", "data": {"docs": []}}
    yield {"event": "done", "data": {"thread_id": thread_id, "usage": {"input_tokens": 1}}}


def parse_sse(raw: str) -> list[tuple[str, dict]]:
    """按 `event: x\\ndata: {...}` 解析 SSE 帧，顺带校验帧结构严格性。"""
    frames: list[tuple[str, dict]] = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        assert len(lines) == 2, f"帧必须是两行，实际 {len(lines)}: {block!r}"
        assert lines[0].startswith("event: "), block
        assert lines[1].startswith("data: "), block
        frames.append((lines[0][7:], json.loads(lines[1][6:])))
    return frames


# ---------------------------------------------------------------- 用例


async def test_units() -> None:
    print("[1] 格式化与代码归一化")
    check("money(1705.3)", money(Decimal("1705.3")) == "1705.30")
    check("thousands(1705.3)", thousands(Decimal("1705.3")) == "1,705.30")
    check("thousands(负)", thousands(Decimal("-12345.6")) == "-12,345.60")
    check("with_sign(正)", with_sign(Decimal("1.234")) == "+1.23")
    check("with_sign(负零归一)", with_sign(Decimal("-0.001")) == "0.00")
    check(
        "normalize_code(带空格)",
        normalize_code("SH 600519") == ("SH600519", True),
    )
    check("normalize_code(裸码)", normalize_code("000001") == ("SZ000001", True))
    check("normalize_code(北交所)", normalize_code("920001") == ("BJ920001", True))
    check("normalize_code(非法)", normalize_code("abc") == ("", False))
    check("display_code", display_code("SH600519") == "SH 600519")


async def test_envelope_and_auth(app) -> None:
    print("[2] 信封 / 鉴权 / 自选列表")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")
        payload = resp.json()
        check("healthz 200", resp.status_code == 200)
        check("healthz 信封顺序", list(payload) == ["code", "message", "data"], str(payload))
        check("healthz code=0 message=ok", payload["code"] == 0 and payload["message"] == "ok")
        check("healthz data", payload["data"] == {"status": "ok"}, str(payload["data"]))
        check("X-Request-ID 回填", bool(resp.headers.get("X-Request-ID")))

        resp = await client.get("/api/v1/watchlist")
        check("无 token → 401", resp.status_code == 401, str(resp.status_code))
        check(
            "401 文案",
            resp.json()["message"] == "未登录或登录已失效",
            resp.text,
        )

        resp = await client.get(
            "/api/v1/watchlist", headers={"Authorization": "Bearer not-a-jwt"}
        )
        check("坏 token → 401", resp.status_code == 401, str(resp.status_code))

        resp = await client.get("/api/v1/watchlist", headers=auth_headers())
        body = resp.json()
        check("自选列表 200", resp.status_code == 200, resp.text)
        check("列表为空数组", body["data"]["list"] == [], str(body["data"]))
        check("总盈亏 0.00", body["data"]["total_profit"] == "0.00")
        check("涨跌家数为 0", body["data"]["up_count"] == 0 and body["data"]["down_count"] == 0)

        resp = await client.get("/api/v1/stock/quote?code=abc", headers=auth_headers())
        check("非法代码 → 400", resp.status_code == 400, resp.text)
        check(
            "400 信封",
            resp.json()["code"] == 400 and resp.json()["data"] is None,
            resp.text,
        )


async def test_sse(app) -> None:
    print("[3] 对话 SSE")

    async def _noop_upsert(user_id: str, session_id: str, title: str) -> None:
        """旁路会话注册：本地没装 agent 依赖，替成空实现。"""
        return None

    original = agent_bridge.stream_events
    original_upsert = agent_bridge.upsert_session
    agent_bridge.stream_events = _fake_stream  # type: ignore[assignment]
    agent_bridge.upsert_session = _noop_upsert  # type: ignore[assignment]
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with client.stream(
                "POST",
                "/api/v1/agent/stream",
                headers=auth_headers(),
                json={"query": "茅台现在多少钱", "session_id": "sess_smoke_1"},
            ) as resp:
                content_type = resp.headers.get("content-type", "")
                check(
                    "Content-Type 是 text/event-stream",
                    content_type.startswith("text/event-stream"),
                    content_type,
                )
                check(
                    "X-Accel-Buffering: no",
                    resp.headers.get("x-accel-buffering") == "no",
                )
                raw = (await resp.aread()).decode("utf-8")

            frames = parse_sse(raw)
            names = [name for name, _ in frames]
            check("事件顺序完整", names == ["status", "token", "card", "sources", "done"], str(names))
            check(
                "token.delta",
                frames[1][1] == {"delta": "你好"},
                str(frames[1][1]),
            )
            check(
                "done.usage",
                frames[-1][1]["usage"]["input_tokens"] == 1,
                str(frames[-1][1]),
            )

            resp = await client.post(
                "/api/v1/agent/stream",
                headers=auth_headers(),
                json={"query": "   "},
            )
            check("空 query → 400", resp.status_code == 400, resp.text)

            resp = await client.post(
                "/api/v1/agent/stream",
                headers=auth_headers(),
                json={"query": "长" * 2001},
            )
            check("超长 query → 400", resp.status_code == 400, resp.text)
            check(
                "超长文案",
                resp.json()["message"] == "提问过长，最多 2000 字",
                resp.text,
            )
    finally:
        agent_bridge.stream_events = original  # type: ignore[assignment]
        agent_bridge.upsert_session = original_upsert  # type: ignore[assignment]


async def test_rate_limit() -> None:
    print("[4] 限流（真实执行 Lua）")
    cfg = make_config()
    cfg.ratelimit.ip_per_min = 2
    redis = fake_aioredis.FakeRedis(decode_responses=True)
    app, _ = build_app(cfg, redis)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # healthz 不参与计数
        for _ in range(3):
            resp = await client.get("/healthz")
            check("healthz 不被限流", resp.status_code == 200, str(resp.status_code))

        first = await client.get("/api/v1/watchlist", headers=auth_headers())
        second = await client.get("/api/v1/watchlist", headers=auth_headers())
        third = await client.get("/api/v1/watchlist", headers=auth_headers())
        check("第 1 次放行", first.status_code == 200, str(first.status_code))
        check("第 2 次放行", second.status_code == 200, str(second.status_code))
        check("第 3 次 429", third.status_code == 429, str(third.status_code))
        check(
            "429 信封",
            third.json()["code"] == 429
            and third.json()["message"] == "请求过于频繁，请稍后再试",
            third.text,
        )
    await redis.aclose()


async def test_agent_limiter() -> None:
    print("[5] 对话三道闸门")
    redis = fake_aioredis.FakeRedis(decode_responses=True)
    limiter = AgentLimiter(redis, qps=1, concurrent=1, daily_quota=2)

    # 闸门一：QPS（capacity = qps = 1，用完立刻再来一颗都没有）
    check("首次放行", await limiter.allow(1) is None)
    check("QPS 桶空 → 限流", await limiter.allow(1) == "rate_limit")
    await limiter.release_concurrent(1)

    # 闸门二：并发（占着槽再来一次要被拒，且不能把配额吃掉）
    await asyncio.sleep(1.1)  # 让令牌桶补满
    check("并发闸门放行", await limiter.allow(2) is None)  # conc=1 / quota=1
    await asyncio.sleep(1.1)
    check("并发超限", await limiter.allow(2) == "too_many_concurrent")
    await limiter.release_concurrent(2)

    # 闸门三：日配额（当天第 2 次放行，第 3 次用尽）
    await asyncio.sleep(1.1)
    check("配额还剩 1", await limiter.allow(2) is None)  # conc=1 / quota=2
    await limiter.release_concurrent(2)
    await asyncio.sleep(1.1)
    check("配额用尽", await limiter.allow(2) == "quota_exceeded")
    await redis.aclose()


async def main() -> None:
    await test_units()

    cfg = make_config()
    cfg.ratelimit.ip_per_min = 10_000
    redis = fake_aioredis.FakeRedis(decode_responses=True)
    app, _ = build_app(cfg, redis)

    await test_envelope_and_auth(app)
    await test_sse(app)
    await redis.aclose()

    await test_rate_limit()
    await test_agent_limiter()

    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + ", ".join(failures))
        raise SystemExit(1)
    print("ALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
