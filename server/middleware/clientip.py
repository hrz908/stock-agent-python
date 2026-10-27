"""真实客户端 IP 解析。

对齐 Go 版 gin.SetTrustedProxies 的语义：只有直连对端落在可信反代网段内，
才采信 X-Forwarded-For / X-Real-IP；否则一律用 TCP 对端地址。

生产环境 nginx 在宿主机、网关在容器里，来源是 docker 网桥（172.16.0.0/12），
所以 TRUSTED_PROXIES 必须把网段配进来——只信 127.0.0.1 会让全站共用一个
限流桶，按 IP 限流形同虚设。
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache

from starlette.requests import Request


@lru_cache(maxsize=8)
def _parse_networks(raw: str) -> tuple:
    nets = []
    for part in (raw or "").split(","):
        text = part.strip()
        if not text:
            continue
        try:
            nets.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            # 非法条目忽略：不能因为一条配置写错就把整个限流关掉
            continue
    if not nets:
        nets.append(ipaddress.ip_network("127.0.0.1/32"))
    return tuple(nets)


def _is_trusted(ip: str, raw: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _parse_networks(raw))


def resolve_client_ip(
    peer: str, forwarded_for: str, real_ip: str, trusted_proxies: str
) -> str:
    """由 TCP 对端地址 + 两个反代头解析真实客户端 IP。"""
    if not peer:
        return ""
    if not _is_trusted(peer, trusted_proxies):
        return peer
    if forwarded_for:
        # 取最左侧（最接近真实客户端的）一跳
        candidate = forwarded_for.split(",")[0].strip()
        if candidate:
            return candidate
    return (real_ip or "").strip() or peer


def client_ip(request: Request, trusted_proxies: str) -> str:
    return resolve_client_ip(
        request.client.host if request.client else "",
        request.headers.get("x-forwarded-for", ""),
        request.headers.get("x-real-ip", ""),
        trusted_proxies,
    )
