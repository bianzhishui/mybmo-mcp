from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mybmo_mcp.baidu.errors import BaiduApiError, DryRunRequested, redact
from mybmo_mcp.baidu.transport import (
    SMS_BASE,
    BaiduTransport,
    RequestCredentials,
    StaticCredentials,
    is_write_method,
    normalize_method,
    normalize_service,
)

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "tok-secret-value"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def build(handler: Any, **kwargs: Any) -> BaiduTransport:
    """用 MockTransport 构造传输层：不联网，退避与限速都关掉。"""
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    creds = StaticCredentials(RequestCredentials(user_name="示例账户", access_token=TOKEN))
    kwargs.setdefault("min_interval", 0)
    kwargs.setdefault("backoff_base", 0)
    return BaiduTransport(creds, client=client, **kwargs)


async def test_success_signs_payload_and_returns_data() -> None:
    seen: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, text=fixture_text("account_info.json"))

    transport = build(handler)
    data = await transport.call("accountService", "get_account_info", {"userId": 1000001})
    await transport.aclose()

    assert len(seen) == 1
    assert str(seen[0].url) == f"{SMS_BASE}/AccountService/getAccountInfo"

    payload = json.loads(seen[0].content)
    assert payload["header"] == {
        "userName": "示例账户",
        "accessToken": TOKEN,
        "action": "API-PY-SDK",
    }
    assert payload["body"] == {"userId": 1000001}

    assert data[0]["userName"] == "示例科技（已脱敏）"
    assert transport.last_rquota == {"quota": 1000, "used": 37, "limit": 1000}


async def test_method_name_is_normalized_before_sending() -> None:
    urls: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        urls.append(str(request.url))
        return httpx2.Response(200, text=fixture_text("kr_expand.json"))

    transport = build(handler)
    await transport.call("krService", "get_kr_by_query", {"query": "月季"})

    assert urls == [f"{SMS_BASE}/KRService/getKRByQuery"]


async def test_business_error_carries_code_and_hint() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text=fixture_text("error_81020.json"))

    with pytest.raises(BaiduApiError) as excinfo:
        await build(handler).call("accountService", "getAccountInfo")

    err = excinfo.value
    assert err.code == 81020
    assert err.position == "header.userName"
    assert err.outcome == "failed"
    assert err.hint is not None and "不属于同一个账户" in err.hint


async def test_read_timeout_retries_then_reports_unknown() -> None:
    attempts = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        raise httpx2.ReadTimeout("timed out")

    with pytest.raises(BaiduApiError) as excinfo:
        await build(handler).call("accountService", "getAccountInfo")

    assert attempts == 3  # 首次 + 2 次重试
    assert excinfo.value.outcome == "unknown"


async def test_write_timeout_is_not_retried() -> None:
    attempts = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        raise httpx2.ReadTimeout("timed out")

    with pytest.raises(BaiduApiError) as excinfo:
        await build(handler).call("keywordService", "add_word", {"words": ["月季"]})

    assert attempts == 1  # 写请求绝不重发
    assert excinfo.value.outcome == "unknown"


async def test_http_500_is_unknown_and_not_retried() -> None:
    attempts = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        return httpx2.Response(500, text="bad gateway")

    with pytest.raises(BaiduApiError) as excinfo:
        await build(handler).call("accountService", "getAccountInfo")

    assert attempts == 1
    assert excinfo.value.outcome == "unknown"


async def test_missing_header_status_is_unknown() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text='{"body": {"data": []}}')

    with pytest.raises(BaiduApiError) as excinfo:
        await build(handler).call("accountService", "getAccountInfo")

    assert excinfo.value.outcome == "unknown"


async def test_dry_run_sends_nothing_and_masks_token() -> None:
    attempts = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        raise AssertionError("dry-run 不应发出请求")

    with pytest.raises(DryRunRequested) as excinfo:
        await build(handler).call("keywordService", "addWord", {"words": ["月季"]}, dry_run=True)

    assert attempts == 0
    rendered = excinfo.value.to_dict()
    assert rendered["payload"]["header"]["accessToken"] == "***"
    assert rendered["url"].endswith("/KeywordService/addWord")
    assert TOKEN not in json.dumps(rendered, ensure_ascii=False)


def test_redact_masks_credentials_but_keeps_error_codes() -> None:
    text = "accessToken=abc123 refreshToken: def456 secret_key=ghi auth_code=jkl"

    masked = redact(text)

    for raw in ("abc123", "def456", "ghi", "jkl"):
        assert raw not in masked
    assert redact("status=0 code=8002") == "status=0 code=8002"


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("getAccountInfo", False),
        ("queryReport", False),
        ("listEntities", False),
        ("addWord", True),
        ("updateBid", True),
        ("cancelDownload", True),
        ("deleteCampaign", True),
    ],
)
def test_is_write_method(method: str, expected: bool) -> None:
    assert is_write_method(method) is expected


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("krService", "KRService"),
        ("accountService", "AccountService"),
        ("get_kr_by_query", "getKRByQuery"),
        ("getKRByQuery", "getKRByQuery"),
        ("get_account_info", "getAccountInfo"),
    ],
)
def test_name_normalization_is_idempotent(raw: str, canonical: str) -> None:
    normalized = (
        normalize_service(raw) if raw.lower().endswith("service") else normalize_method(raw)
    )
    assert normalized == canonical
    again = (
        normalize_service(normalized)
        if normalized.lower().endswith("service")
        else normalize_method(normalized)
    )
    assert again == canonical
