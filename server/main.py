"""FastAPI 应用入口（网关 + agent 合并为单进程）。

部署契约保持不变：容器内监听 8080、/healthz 返回 code:0、头像走 /uploads/avatars。
nginx 的 upstream、scripts/deploy.sh 探活、watchdog.sh 全部无需改动。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from server import agent_bridge
from server.agent_limiter import AgentLimiter
from server.config import Config, load_config
from server.db.postgres import Postgres
from server.db.redis import create_redis, ping_redis
from server.deps import Runtime, clear_runtime, set_runtime
from server.middleware.gateway import GatewayMiddleware
from server.middleware.guards import market_rate_limit
from server.middleware.rate_limit import SlidingWindowLimiter
from server.modules import auth as auth_module
from server.modules import chat as chat_module
from server.modules import stock as stock_module
from server.modules import user as user_module
from server.modules import watchlist as watchlist_module
from server.quote.source import MarketSource
from server.resp import (
    CODE_BAD_REQUEST,
    CODE_INTERNAL,
    ApiError,
    api_error_handler,
    body,
    http_status,
    ok_response,
)
from server.security.auth import require_auth
from server.wechat.session import WeChatClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("stocka.server")

SCHEMA_FILE = Path(__file__).with_name("schema.sql")
API_PREFIX = "/api/v1"


def _check_prod_config(cfg: Config) -> None:
    """prod 安全底线：JWT 密钥仍是默认值等于人人可伪造 token；
    微信凭据缺失会让所有用户共用一个演示账号、数据串号。

    dev 两者都允许（本地联调方便），prod 一律拒绝启动。
    """
    if cfg.env != "prod":
        return
    if not cfg.jwt_secret or cfg.jwt_secret == "please-change-me":
        raise SystemExit("JWT_SECRET 未设置或仍是默认值，禁止以 prod 启动")
    if not cfg.wechat_appid or not cfg.wechat_secret:
        raise SystemExit(
            "WECHAT_APPID / WECHAT_SECRET 未配置，禁止以 prod 启动"
            "（否则所有用户共用一个演示账号）"
        )


async def _apply_schema(pg: Postgres) -> None:
    """幂等建表，等价于原 Go 版 gorm AutoMigrate 对这三张表的处理。"""
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    async with pg.pool.connection() as conn:
        await conn.execute(sql)
    logger.info("gateway schema ensured")


def _build_api_router() -> APIRouter:
    """按「公开 / 已鉴权 / 行情限流」三层组装路由，顺序同 Go 版 router.go。"""
    api = APIRouter(prefix=API_PREFIX)

    # 公开：登录（内部另挂更严的 IP 限流）
    api.include_router(auth_module.router)

    # 已鉴权：user_id 只由 require_auth 从 Bearer 解出
    protected = APIRouter(dependencies=[Depends(require_auth)])
    protected.include_router(chat_module.router)
    protected.include_router(watchlist_module.router)
    protected.include_router(user_module.router)
    api.include_router(protected)

    # 行情 / 搜索会放大成多个第三方上游请求，按用户单独限流
    market = APIRouter(dependencies=[Depends(require_auth), Depends(market_rate_limit)])
    market.include_router(stock_module.router)
    api.include_router(market)

    return api


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    _check_prod_config(cfg)

    # 头像目录必须在 mount 之前存在，否则 StaticFiles 构造即报错
    avatar_dir = cfg.upload_path / "avatars"
    avatar_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pg = Postgres(cfg.dsn, cfg.db)
        await pg.open()
        await _apply_schema(pg)

        redis_client = create_redis(cfg)
        await ping_redis(redis_client)

        limiter = SlidingWindowLimiter(redis_client)
        rt = Runtime(cfg=cfg, pg=pg, redis=redis_client, limiter=limiter)
        rt.services["wechat"] = WeChatClient(
            cfg.wechat_appid, cfg.wechat_secret, cfg.http.wechat_timeout
        )
        rt.services["market_source"] = MarketSource(cfg.http, cfg.quote_api)
        rt.services["agent_limiter"] = AgentLimiter(
            redis_client, cfg.agent.qps, cfg.agent.concurrent, cfg.agent.daily_quota
        )
        # 合并后没有 HTTP 代理层，原 proxy_concurrency 的语义变成「全局同时运行的上限」
        rt.services["run_semaphore"] = asyncio.Semaphore(
            max(1, cfg.agent.proxy_concurrency)
        )
        set_runtime(rt)
        app.state.runtime = rt

        # 合并后 agent 与本进程同生共死：图/checkpointer/工具集预热都在这里
        try:
            await agent_bridge.startup(rt)
        except Exception as exc:  # noqa: BLE001 - 预热失败不阻断网关启动
            logger.warning("agent startup failed: %s", exc)

        try:
            yield
        finally:
            try:
                await agent_bridge.shutdown()
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent shutdown failed: %s", exc)
            for name in ("market_source", "wechat"):
                service = rt.services.get(name)
                if service is None:
                    continue
                try:
                    await service.close()
                except Exception:  # noqa: BLE001
                    pass
            await redis_client.aclose()
            await pg.close()
            await clear_runtime()

    app = FastAPI(title="StockA Gateway", lifespan=lifespan, docs_url=None, redoc_url=None)

    # ---- 异常 → 统一信封 ----
    app.add_exception_handler(ApiError, api_error_handler)

    async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        logger.info("request validation failed: %s", exc.errors())
        return JSONResponse(
            content=body(CODE_BAD_REQUEST, "请求参数不合法", None),
            status_code=http_status(CODE_BAD_REQUEST),
        )

    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            content=body(CODE_INTERNAL, "服务内部错误", None),
            status_code=http_status(CODE_INTERNAL),
        )

    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(Exception, _unhandled_handler)

    # ---- 全局中间件（单一 ASGI 组件，顺序在内部固定） ----
    def _limiter_getter() -> SlidingWindowLimiter | None:
        rt = getattr(app.state, "runtime", None)
        return rt.limiter if rt is not None else None

    app.add_middleware(GatewayMiddleware, cfg=cfg, limiter_getter=_limiter_getter)

    # ---- 路由 ----
    @app.get("/healthz")
    async def health():
        return ok_response({"status": "ok"})

    # 只暴露 avatars 子目录：挂整个 upload 目录会让目录路径返回文件列表（信息泄露）
    app.mount("/uploads/avatars", StaticFiles(directory=str(avatar_dir)), name="avatars")

    app.include_router(_build_api_router())

    return app


app = create_app()
