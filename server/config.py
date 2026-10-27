"""统一配置层：优先级 环境变量 > config.yaml（可选）> 内置默认值。

键名与 Go 版本完全一致（addr / dsn / jwt_secret / server.read_timeout …），
环境变量映射规则同 viper：点换下划线并大写（server.read_timeout -> SERVER_READ_TIMEOUT）。

时长字段在配置对象里统一存「秒（float）」，解析支持 Go 的 time.ParseDuration 语法
（1.5s / 500ms / 168h）；纯数字按秒处理。非法值直接抛错终止启动，
不允许静默回退——配置写错了要在启动时炸，而不是运行期才暴露。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_DURATION_PART = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ns|us|µs|ms|s|m|h)")
_UNIT_SECONDS = {
    "ns": 1e-9,
    "us": 1e-6,
    "µs": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}


def parse_duration(raw: Any) -> float:
    """把 Go 风格时长字符串解析成秒；数字原样按秒返回。"""
    if isinstance(raw, bool):
        raise ValueError(f"invalid duration: {raw!r}")
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text:
        raise ValueError("empty duration")
    sign = 1.0
    if text[0] in "+-":
        if text[0] == "-":
            sign = -1.0
        text = text[1:]
    if text == "0":
        return 0.0
    pos = 0
    total = 0.0
    for match in _DURATION_PART.finditer(text):
        if match.start() != pos:
            raise ValueError(f"invalid duration: {raw!r}")
        total += float(match.group(1)) * _UNIT_SECONDS[match.group(2)]
        pos = match.end()
    if pos == 0 or pos != len(text):
        raise ValueError(f"invalid duration: {raw!r}")
    return sign * total


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    return int(str(value).strip()) if isinstance(value, str) else int(value)


def _as_str(value: Any) -> str:
    return "" if value is None else str(value)


def _load_dotenv(path: Path) -> None:
    """把 .env 注入进程环境；已存在的真实环境变量优先（不覆盖）。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(node, dict):
        return out
    for key, value in node.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, name + "."))
        else:
            out[name] = value
    return out


def _lookup(flat: dict[str, Any], key: str, default: Any) -> Any:
    env_key = key.upper().replace(".", "_")
    if env_key in os.environ:
        return os.environ[env_key]
    if key in flat:
        return flat[key]
    return default


@dataclass(slots=True)
class ServerConfig:
    read_header_timeout: float = 5.0
    read_timeout: float = 15.0
    write_timeout: float = 15.0
    idle_timeout: float = 60.0
    shutdown_timeout: float = 5.0


@dataclass(slots=True)
class DBConfig:
    max_open_conns: int = 25
    max_idle_conns: int = 25
    conn_max_lifetime: float = 300.0
    conn_max_idle_time: float = 300.0


@dataclass(slots=True)
class RedisConfig:
    pool_size: int = 0


@dataclass(slots=True)
class RateLimitConfig:
    ip_per_min: int = 100
    login_per_min: int = 10
    market_per_min: int = 120


@dataclass(slots=True)
class AgentConfig:
    qps: int = 2
    concurrent: int = 3
    daily_quota: int = 500
    proxy_concurrency: int = 64
    run_timeout: float = 120.0
    ttft_timeout: float = 8.0
    max_line_mb: int = 1


@dataclass(slots=True)
class HTTPConfig:
    quote_timeout: float = 3.0
    search_timeout: float = 5.0
    wechat_timeout: float = 5.0
    wecom_timeout: float = 5.0
    quote_cache_ttl: float = 30.0
    quote_fetch_limit: int = 5
    quote_batch_size: int = 20
    quote_mget_chunk: int = 100


@dataclass(slots=True)
class Config:
    addr: str = ":8080"
    dsn: str = "postgres://postgres:postgres@localhost:5432/stock_agent?sslmode=disable"
    jwt_secret: str = "please-change-me"
    jwt_ttl: float = 7 * 24 * 3600.0
    redis_addr: str = "127.0.0.1:6379"
    redis_password: str = ""
    redis_db: int = 0

    quote_api: str = ""
    wechat_appid: str = ""
    wechat_secret: str = ""
    wecom_corp_id: str = ""
    wecom_secret: str = ""
    wecom_agent_id: int = 0
    wecom_webhook: str = ""

    upload_dir: str = "./uploads"
    env: str = "dev"
    allow_origins: str = "*"
    trusted_proxies: str = "127.0.0.1"

    # 合并为单进程后不再是 HTTP 上游地址；保留键名以兼容既有 .env，
    # 但网关只在本进程内调用 agent，不再发起 HTTP 请求。
    agent_base_url: str = "http://127.0.0.1:8000"

    watchlist_max_items: int = 200

    server: ServerConfig = field(default_factory=ServerConfig)
    db: DBConfig = field(default_factory=DBConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    ratelimit: RateLimitConfig = field(default_factory=RateLimitConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    http: HTTPConfig = field(default_factory=HTTPConfig)

    @property
    def host(self) -> str:
        """从 ":8080" / "0.0.0.0:8080" 解析出监听地址。"""
        addr = self.addr.strip()
        if addr.startswith(":"):
            return "0.0.0.0"
        if ":" in addr:
            return addr.rsplit(":", 1)[0]
        return addr

    @property
    def port(self) -> int:
        addr = self.addr.strip()
        if ":" not in addr:
            return 8080
        return _as_int(addr.rsplit(":", 1)[1])

    @property
    def upload_path(self) -> Path:
        return Path(self.upload_dir)


def _build(flat: dict[str, Any]) -> Config:
    def get(key: str, default: Any) -> Any:
        return _lookup(flat, key, default)

    cfg = Config(
        addr=_as_str(get("addr", ":8080")),
        dsn=_as_str(get("dsn", Config.dsn)),
        jwt_secret=_as_str(get("jwt_secret", "please-change-me")),
        jwt_ttl=parse_duration(get("jwt_ttl", "168h")),
        redis_addr=_as_str(get("redis_addr", "127.0.0.1:6379")),
        redis_password=_as_str(get("redis_password", "")),
        redis_db=_as_int(get("redis_db", 0)),
        quote_api=_as_str(get("quote_api", "")),
        wechat_appid=_as_str(get("wechat_appid", "")),
        wechat_secret=_as_str(get("wechat_secret", "")),
        wecom_corp_id=_as_str(get("wecom_corp_id", "")),
        wecom_secret=_as_str(get("wecom_secret", "")),
        wecom_agent_id=_as_int(get("wecom_agent_id", 0)),
        wecom_webhook=_as_str(get("wecom_webhook", "")),
        upload_dir=_as_str(get("upload_dir", "./uploads")),
        env=_as_str(get("env", "dev")),
        allow_origins=_as_str(get("allow_origins", "*")),
        trusted_proxies=_as_str(get("trusted_proxies", "127.0.0.1")),
        agent_base_url=_as_str(get("agent_base_url", "http://127.0.0.1:8000")),
        watchlist_max_items=_as_int(get("watchlist_max_items", 200)),
    )

    cfg.server = ServerConfig(
        read_header_timeout=parse_duration(get("server.read_header_timeout", "5s")),
        read_timeout=parse_duration(get("server.read_timeout", "15s")),
        write_timeout=parse_duration(get("server.write_timeout", "15s")),
        idle_timeout=parse_duration(get("server.idle_timeout", "60s")),
        shutdown_timeout=parse_duration(get("server.shutdown_timeout", "5s")),
    )
    cfg.db = DBConfig(
        max_open_conns=_as_int(get("db.max_open_conns", 25)),
        max_idle_conns=_as_int(get("db.max_idle_conns", 25)),
        conn_max_lifetime=parse_duration(get("db.conn_max_lifetime", "5m")),
        conn_max_idle_time=parse_duration(get("db.conn_max_idle_time", "5m")),
    )
    cfg.redis = RedisConfig(pool_size=_as_int(get("redis.pool_size", 0)))
    cfg.ratelimit = RateLimitConfig(
        ip_per_min=_as_int(get("ratelimit.ip_per_min", 100)),
        login_per_min=_as_int(get("ratelimit.login_per_min", 10)),
        market_per_min=_as_int(get("ratelimit.market_per_min", 120)),
    )
    cfg.agent = AgentConfig(
        qps=_as_int(get("agent.qps", 2)),
        concurrent=_as_int(get("agent.concurrent", 3)),
        daily_quota=_as_int(get("agent.daily_quota", 500)),
        proxy_concurrency=_as_int(get("agent.proxy_concurrency", 64)),
        run_timeout=parse_duration(get("agent.run_timeout", "120s")),
        ttft_timeout=parse_duration(get("agent.ttft_timeout", "8s")),
        max_line_mb=_as_int(get("agent.max_line_mb", 1)),
    )
    cfg.http = HTTPConfig(
        quote_timeout=parse_duration(get("http.quote_timeout", "3s")),
        search_timeout=parse_duration(get("http.search_timeout", "5s")),
        wechat_timeout=parse_duration(get("http.wechat_timeout", "5s")),
        wecom_timeout=parse_duration(get("http.wecom_timeout", "5s")),
        quote_cache_ttl=parse_duration(get("http.quote_cache_ttl", "30s")),
        quote_fetch_limit=_as_int(get("http.quote_fetch_limit", 5)),
        quote_batch_size=_as_int(get("http.quote_batch_size", 20)),
        quote_mget_chunk=_as_int(get("http.quote_mget_chunk", 100)),
    )
    return cfg


def load_config(dotenv: str = ".env", config_file: str = "config.yaml") -> Config:
    """加载配置：.env → config.yaml（可选）→ 环境变量覆盖。"""
    _load_dotenv(Path(dotenv))

    flat: dict[str, Any] = {}
    path = Path(config_file)
    if path.is_file():
        parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        flat = _flatten(parsed)

    return _build(flat)
