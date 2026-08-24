from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True)
class ExternalIdentity:
    openid: str
    nickname: str


class AuthProvider(Protocol):
    def exchange_code(self, code: str) -> ExternalIdentity: ...


class FakeAuthProvider:
    """Test-account login used until the WeChat provider is available."""

    def exchange_code(self, code: str) -> ExternalIdentity:
        if not code.startswith("test-"):
            raise ValueError("invalid fake login code")
        return ExternalIdentity(openid=f"fake:{code}", nickname="测试家长")


class WeChatAuthProvider:
    """Exchange a short-lived ``wx.login`` code through code2Session."""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._client = client or httpx.Client(timeout=10.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def exchange_code(self, code: str) -> ExternalIdentity:
        try:
            response = self._client.get(
                "https://api.weixin.qq.com/sns/jscode2session",
                params={
                    "appid": self._app_id,
                    "secret": self._app_secret,
                    "js_code": code,
                    "grant_type": "authorization_code",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ValueError("wechat login service unavailable") from error
        openid = payload.get("openid") if isinstance(payload, dict) else None
        if not isinstance(openid, str) or not openid:
            raise ValueError("wechat login code rejected")
        return ExternalIdentity(openid=openid, nickname="")
