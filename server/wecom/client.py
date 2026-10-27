"""企业微信消息推送（自建应用消息 + 群机器人两条通道）。

用途：把命中的股票提醒推给企业成员。自建应用只能发给企业内部 userid，
而小程序用户表只有 openid、无法映射，因此统一推给 @all。

两条通道相互独立：应用消息需要 corpid/secret/agentid 且受「企业可信 IP」限制；
群机器人只要一个 webhook，可作为应用消息不可用时的替代。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("stocka.server.wecom")

TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"

# 官方 7200s，提前 5 分钟判过期，避免用到临界失效的 token
TOKEN_REFRESH_AHEAD = 300.0
FALLBACK_TOKEN_TTL = 7200.0
MAX_RESP_BYTES = 64 << 10
# token 失效类错误码：清缓存后下次调用重新获取
TOKEN_INVALID_CODES = frozenset({40014, 41001, 42001})


class WeComClient:
    def __init__(
        self, corp_id: str, secret: str, agent_id: int, webhook: str, timeout: float = 5.0
    ) -> None:
        self._corp_id = corp_id or ""
        self._secret = secret or ""
        self._agent_id = agent_id or 0
        self._webhook = (webhook or "").strip()
        self._client = httpx.AsyncClient(timeout=timeout if timeout > 0 else 5.0)
        self._lock = asyncio.Lock()
        self._token = ""
        self._token_expire = 0.0

    @property
    def enabled(self) -> bool:
        """未配置凭据时不要调接口，否则每条提醒都白等一次网络失败。"""
        return bool(self._corp_id and self._secret and self._agent_id)

    @property
    def webhook_enabled(self) -> bool:
        return bool(self._webhook)

    async def close(self) -> None:
        await self._client.aclose()

    async def _do_json(
        self, method: str, url: str, payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        # 每次调用独立 5s 超时：推送跑在提醒检查的后台任务里，挂着不返回会占住任务
        response = await self._client.request(method, url, json=payload, timeout=5.0)
        body = response.content[:MAX_RESP_BYTES]
        if not body:
            return {}
        import json

        return json.loads(body.decode("utf-8", "replace"))

    async def _access_token(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._token_expire:
                return self._token

            data = await self._get_token()
            token = str(data.get("access_token") or "")
            errcode = int(data.get("errcode") or 0)
            if errcode != 0 or not token:
                raise RuntimeError(
                    f"gettoken failed: errcode={errcode} errmsg={data.get('errmsg', '')}"
                )

            ttl = float(data.get("expires_in") or 0) or FALLBACK_TOKEN_TTL
            if ttl > TOKEN_REFRESH_AHEAD:
                ttl -= TOKEN_REFRESH_AHEAD
            self._token = token
            self._token_expire = time.monotonic() + ttl
            return token

    async def _get_token(self) -> dict[str, Any]:
        response = await self._client.get(
            TOKEN_URL,
            params={"corpid": self._corp_id, "corpsecret": self._secret},
            timeout=5.0,
        )
        body = response.content[:MAX_RESP_BYTES]
        if not body:
            return {}
        import json

        return json.loads(body.decode("utf-8", "replace"))

    async def invalidate_token(self) -> None:
        async with self._lock:
            self._token = ""
            self._token_expire = 0.0

    async def send_text_to_all(self, content: str) -> None:
        """向全体成员（@all）推送文本；未配置凭据时静默跳过。"""
        if not self.enabled:
            return
        if not content:
            raise ValueError("empty content")

        token = await self._access_token()
        data = await self._do_json(
            "POST",
            f"{SEND_URL}?access_token={token}",
            {
                "touser": "@all",
                "msgtype": "text",
                "agentid": self._agent_id,
                "text": {"content": content},
            },
        )
        errcode = int(data.get("errcode") or 0)
        if errcode != 0:
            if errcode in TOKEN_INVALID_CODES:
                await self.invalidate_token()
            raise RuntimeError(
                f"send message failed: errcode={errcode} errmsg={data.get('errmsg', '')}"
            )

    async def send_text_to_webhook(self, content: str) -> None:
        """群机器人推送：不需要 access_token，也不受「企业可信 IP」限制。"""
        if not self._webhook:
            return
        if not content:
            raise ValueError("empty content")

        try:
            data = await self._do_json(
                "POST", self._webhook, {"msgtype": "text", "text": {"content": content}}
            )
        except Exception as exc:
            # webhook URL 里的 key 等同密码，错误文本会带上完整 URL，必须脱敏后再往外抛
            raise RuntimeError(self._mask(str(exc))) from exc

        errcode = int(data.get("errcode") or 0)
        if errcode != 0:
            raise RuntimeError(
                f"webhook send failed: errcode={errcode} errmsg={data.get('errmsg', '')}"
            )

    def _mask(self, message: str) -> str:
        if not self._webhook:
            return message
        return message.replace(self._webhook, "<webhook>")
