from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from mybmo_mcp.auth.oauth import BaiduOAuth, build_authorize_url
from mybmo_mcp.baidu.errors import CredentialError

AUTH_CODE = "auth-code-should-never-leak"
SECRET = "secret-key-value"


def oauth_with(handler: Any, **kwargs: Any) -> BaiduOAuth:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return BaiduOAuth("app-1", SECRET, client=client, now=lambda: 1_000_000.0, **kwargs)


async def test_exchange_sends_expected_body_and_maps_tokens() -> None:
    seen: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={
                "code": 0,
                "message": "success",
                "data": {
                    "accessToken": "at-1",
                    "refreshToken": "rt-1",
                    "openId": "open-1",
                    "userId": 1001,
                    "expiresIn": 86400,
                    "refreshExpiresIn": 2592000,
                    "scope": {"tool": True},
                },
            },
        )

    bundle = await oauth_with(handler).exchange(AUTH_CODE, 1001)

    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://u.baidu.com/oauth/accessToken"
    assert body["grantType"] == "auth_code"
    assert body["appId"] == "app-1"
    assert body["userId"] == 1001
    assert bundle.access_token == "at-1"
    assert bundle.refresh_token == "rt-1"
    assert bundle.user_id == 1001
    assert bundle.expires_at == 1_000_000.0 + 86400
    assert bundle.refresh_expires_at == 1_000_000.0 + 2592000
    assert bundle.scope == {"tool": True}


async def test_refresh_returns_rotated_token() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url).endswith("/oauth/refreshToken")
        return httpx2.Response(
            200,
            json={"code": 0, "data": {"accessToken": "at-2", "refreshToken": "rt-2"}},
        )

    bundle = await oauth_with(handler).refresh("rt-1", 1001)

    assert bundle.access_token == "at-2"
    assert bundle.refresh_token == "rt-2"


async def test_business_error_does_not_echo_auth_code() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"code": 5001, "message": "invalid code"})

    with pytest.raises(CredentialError) as excinfo:
        await oauth_with(handler).exchange(AUTH_CODE, 1001)

    message = str(excinfo.value)
    assert "5001" in message
    assert AUTH_CODE not in message


async def test_network_failure_does_not_echo_secret() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("boom")

    with pytest.raises(CredentialError) as excinfo:
        await oauth_with(handler).exchange(AUTH_CODE, 1001)

    message = str(excinfo.value)
    assert AUTH_CODE not in message
    assert SECRET not in message


async def test_missing_access_token_is_rejected() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"code": 0, "data": {"refreshToken": "rt"}})

    with pytest.raises(CredentialError, match="缺少 accessToken"):
        await oauth_with(handler).exchange(AUTH_CODE, 1001)


async def test_user_info_returns_master_name() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url).endswith("/oauth/getUserInfo")
        return httpx2.Response(
            200, json={"code": 0, "data": {"masterName": "示例账户", "userId": 1001}}
        )

    info = await oauth_with(handler).user_info("at-1", user_id=1001, open_id="open-1")

    assert info["masterName"] == "示例账户"


def test_authorize_url_contains_state_and_callback() -> None:
    url = build_authorize_url(
        "app-1", callback="https://mcp.example.com/callback", state="nonce-123"
    )

    assert url.startswith("https://u.baidu.com/oauth/page/index?")
    assert "appId=app-1" in url
    assert "state=nonce-123" in url
    assert "callback=https%3A%2F%2Fmcp.example.com%2Fcallback" in url


@pytest.mark.parametrize("callback", ["https://x/cb?a=1", "https://x/cb%20a"])
def test_authorize_url_rejects_callback_with_query(callback: str) -> None:
    with pytest.raises(CredentialError, match="callback"):
        build_authorize_url("app-1", callback=callback, state="s")
