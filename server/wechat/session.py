"""微信小程序 code2session 封装。

与 Go 版 internal/wechat 对齐的两条关键语义：
- 微信的错误写在 HTTP 200 响应体的 errcode 里，不能只看状态码；
- 响应体限 64KB，防上游异常时超大响应打爆内存。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("stocka.server.wechat")

CODE2SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"
MAX_RESP_BYTES = 64 << 10


class WeChatClient:
    def __init__(self, appid: str, secret: str, timeout: float = 5.0) -> None:
        self._appid = appid or ""
        self._secret = secret or ""
        self._timeout = timeout if timeout > 0 else 5.0
        # 复用连接：登录是高频短请求，省掉每次 TLS 握手
        self._client = httpx.AsyncClient(timeout=self._timeout)

    @property
    def enabled(self) -> bool:
        """未配置凭据时不要调微信接口，否则每个登录请求都白等一次网络失败。"""
        return bool(self._appid and self._secret)

    async def code2session(self, code: str) -> str:
        """用 wx.login 的 code 换 openid；失败抛 RuntimeError。"""
        if not self.enabled:
            raise RuntimeError("wechat appid/secret not configured")

        params = {
            "appid": self._appid,
            "secret": self._secret,
            "js_code": code,
            "grant_type": "authorization_code",
        }
        response = await self._client.get(CODE2SESSION_URL, params=params)
        # 只读前 64KB：上游异常返回超大 body 时也不拖累网关
        payload: dict[str, Any] = {}
        body = response.content[:MAX_RESP_BYTES]
        if body:
            import json

            payload = json.loads(body.decode("utf-8", "replace"))

        errcode = int(payload.get("errcode") or 0)
        openid = str(payload.get("openid") or "").strip()
        if errcode != 0 or not openid:
            raise RuntimeError(
                f"jscode2session failed: errcode={errcode} "
                f"errmsg={str(payload.get('errmsg') or '').strip()}"
            )
        return openid

    async def close(self) -> None:
        await self._client.aclose()
