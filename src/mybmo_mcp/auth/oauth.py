"""百度 OAuth：授权链接、换令牌、刷新、取账户信息。

端点与字段来自既有实现的真实调用记录，不是文档抄写：

- 授权页 ``https://u.baidu.com/oauth/page/index``，参数
  ``platformId`` / ``appId`` / ``scope`` / ``state`` / ``callback``
- 换令牌 ``POST /oauth/accessToken``，body ``appId`` / ``authCode`` / ``secretKey``
  / ``grantType="auth_code"`` / ``userId``
- 刷新 ``POST /oauth/refreshToken``，body ``appId`` / ``refreshToken`` / ``secretKey`` / ``userId``
- 账户信息 ``POST /oauth/getUserInfo``，返回里的 ``masterName`` 才是调 API 要用的 ``userName``
- 响应统一是 ``{code, message, data}``，``code != 0`` 即失败

**错误信息绝不包含 authCode 或令牌**。换令牌失败时把授权码打进日志，
等于把一次可用的授权凭据写进日志文件。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx2

from mybmo_mcp.baidu.errors import CredentialError

OAUTH_BASE = "https://u.baidu.com/oauth"
AUTH_PAGE = f"{OAUTH_BASE}/page/index"
PLATFORM_ID = "4960345965958561794"
DEFAULT_TIMEOUT = 15.0


@dataclass(frozen=True, slots=True)
class TokenBundle:
    """一次换令牌 / 刷新得到的令牌组。"""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    open_id: str = ""
    user_id: int = 0
    expires_at: float = 0.0
    refresh_expires_at: float = 0.0
    scope: dict[str, Any] = field(default_factory=dict)


def build_authorize_url(
    app_id: str,
    *,
    callback: str,
    state: str,
    scope: str = "",
    platform_id: str = PLATFORM_ID,
) -> str:
    """生成授权链接。``callback`` 不能带 ``?`` 或 ``%``，百度侧不允许。"""
    if not app_id:
        raise CredentialError("缺少 appId，无法生成授权链接")
    if not callback:
        raise CredentialError("缺少 callback（公网可达的回调地址）")
    if "?" in callback or "%" in callback:
        raise CredentialError("callback 不能包含 ? 或 %，附加参数请放进 state")
    query = {
        "platformId": platform_id,
        "appId": app_id,
        "scope": scope,
        "state": state,
        "callback": callback,
    }
    return f"{AUTH_PAGE}?{urlencode({k: v for k, v in query.items() if v})}"


class BaiduOAuth:
    """OAuth 客户端。一个实例对应一个百度应用（AppID / SecretKey）。"""

    def __init__(
        self,
        app_id: str,
        secret_key: str,
        *,
        client: httpx2.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        base_url: str = OAUTH_BASE,
        now=time.time,
    ) -> None:
        self._app_id = app_id
        self._secret_key = secret_key
        self._client = client if client is not None else httpx2.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._base_url = base_url.rstrip("/")
        self._now = now

    async def exchange(self, auth_code: str, user_id: int) -> TokenBundle:
        """用回调拿到的授权码换令牌。"""
        if not auth_code:
            raise CredentialError("缺少 authCode，无法换取令牌")
        data = await self._post(
            "accessToken",
            {
                "appId": self._app_id,
                "authCode": auth_code,
                "secretKey": self._secret_key,
                "grantType": "auth_code",
                "userId": int(user_id),
            },
        )
        return self._bundle(data, "accessToken")

    async def refresh(self, refresh_token: str, user_id: int) -> TokenBundle:
        """用 refreshToken 换一对新令牌。百度可能轮换 refreshToken。"""
        if not refresh_token:
            raise CredentialError("没有 refreshToken，需要重新授权")
        data = await self._post(
            "refreshToken",
            {
                "appId": self._app_id,
                "refreshToken": refresh_token,
                "secretKey": self._secret_key,
                "userId": int(user_id),
            },
        )
        return self._bundle(data, "refreshToken")

    async def user_info(
        self, access_token: str, *, user_id: int = 0, open_id: str = ""
    ) -> dict[str, Any]:
        """取账户信息。返回里的 ``masterName`` 即调 API 需要的 ``userName``。"""
        return await self._post(
            "getUserInfo",
            {
                "openId": open_id,
                "accessToken": access_token,
                "userId": int(user_id),
                "needSubList": False,
            },
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> BaiduOAuth:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ---------- 内部 ----------

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/{path}"
        try:
            response = await self._client.post(url, json=payload)
        except httpx2.HTTPError as exc:
            # 只报错误类型，不带 payload——payload 里有 secretKey 与 authCode。
            raise CredentialError(f"{path} 网络失败（{type(exc).__name__}），请稍后重试") from exc

        try:
            result = json.loads(response.text)
        except ValueError as exc:
            raise CredentialError(f"{path} 返回非 JSON，未披露响应内容") from exc

        if not isinstance(result, dict):
            raise CredentialError(f"{path} 返回的 JSON 顶层不是对象")

        if result.get("code") != 0:
            raise CredentialError(
                f"{path} 失败 code={result.get('code')} message={result.get('message')}"
            )

        data = result.get("data")
        if not isinstance(data, dict):
            raise CredentialError(f"{path} 返回的 data 不是对象")
        return data

    def _bundle(self, data: dict[str, Any], action: str) -> TokenBundle:
        access_token = str(data.get("accessToken") or "")
        if not access_token:
            raise CredentialError(f"{action} 响应缺少 accessToken")

        now = self._now()
        expires_in = _int_field(data.get("expiresIn"))
        refresh_expires_in = _int_field(data.get("refreshExpiresIn"))
        scope = data.get("scope")

        return TokenBundle(
            access_token=access_token,
            refresh_token=str(data.get("refreshToken") or ""),
            open_id=str(data.get("openId") or ""),
            user_id=_int_field(data.get("userId")),
            expires_at=now + expires_in if expires_in else 0.0,
            refresh_expires_at=now + refresh_expires_in if refresh_expires_in else 0.0,
            scope=scope if isinstance(scope, dict) else {},
        )


def _int_field(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
