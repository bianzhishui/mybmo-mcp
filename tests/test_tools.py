from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mybmo_mcp.baidu.errors import CredentialError
from mybmo_mcp.baidu.services import accounts as accounts_service
from mybmo_mcp.baidu.transport import BaiduTransport, RequestCredentials, StaticCredentials
from mybmo_mcp.tools import accounts as accounts_tools
from mybmo_mcp.tools import discover, keyword
from mybmo_mcp.tools.runtime import set_runtime

FIXTURES = Path(__file__).parent / "fixtures"


class FakeEntry:
    def to_dict(self) -> dict[str, Any]:
        return {"alias": "axd", "user_name": "示例账户", "usable": True, "is_default": True}


class FakeRegistry:
    def list_accounts(self) -> list[FakeEntry]:
        return [FakeEntry()]


class FakeRuntime:
    def __init__(self, transport: BaiduTransport | None = None) -> None:
        self.registry = FakeRegistry()
        self._transport = transport

    def transport_for(self, alias: str | None = None) -> BaiduTransport:
        assert self._transport is not None, "测试需要先提供 transport"
        return self._transport


def build_transport(routes: dict[str, str]) -> BaiduTransport:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        for suffix, body in routes.items():
            if url.endswith(suffix):
                return httpx2.Response(200, text=body)
        raise AssertionError(f"未预期的请求：{url}")

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    creds = StaticCredentials(RequestCredentials(user_name="示例账户", access_token="tok"))
    return BaiduTransport(creds, client=client, min_interval=0, backoff_base=0)


@pytest.fixture
def runtime() -> Iterator[FakeRuntime]:
    fake = FakeRuntime()
    set_runtime(fake)  # type: ignore[arg-type]
    yield fake
    set_runtime(None)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


async def test_list_accounts_returns_envelope(runtime: FakeRuntime) -> None:
    result = await accounts_tools.list_accounts()

    assert result["ok"] is True
    assert result["data"][0]["alias"] == "axd"


async def test_account_snapshot_through_tool(runtime: FakeRuntime) -> None:
    runtime._transport = build_transport(
        {
            "AccountService/getAccountInfo": fixture_text("account_info.json"),
            "BalanceService/getBalanceInfo": json.dumps(
                {"header": {"status": 0}, "body": {"data": []}}
            ),
        }
    )

    result = await accounts_tools.account_snapshot("axd")

    assert result["ok"] is True
    assert result["data"]["summary"]["userName"] == "示例科技（已脱敏）"


async def test_credential_errors_are_returned_as_structured_failure() -> None:
    class FailingRuntime(FakeRuntime):
        def transport_for(self, alias: str | None = None) -> BaiduTransport:
            raise CredentialError("账户 axd 的授权已失效，需要重新绑定")

    set_runtime(FailingRuntime())  # type: ignore[arg-type]
    try:
        result = await accounts_tools.account_snapshot("axd")
    finally:
        set_runtime(None)

    assert result["ok"] is False
    assert "重新绑定" in result["error"]["message"]


async def test_keyword_plan_through_tool(runtime: FakeRuntime) -> None:
    runtime._transport = build_transport({"KRService/getKRByQuery": fixture_text("kr_expand.json")})

    result = await keyword.keyword_plan(["月季"], limit_per_seed=2, account="axd")

    assert result["ok"] is True
    assert result["data"]["total"] == 2
    assert result["data"]["truncated"] is True


async def test_keyword_plan_rejects_empty_seed(runtime: FakeRuntime) -> None:
    runtime._transport = build_transport({})

    result = await keyword.keyword_plan([" "])

    assert result["ok"] is False
    assert "种子词" in result["error"]["message"]


def test_api_describe_works_without_credentials() -> None:
    set_runtime(None)

    result = discover.api_describe("krService", "get_kr_by_query")

    assert result["ok"] is True
    assert result["data"]["method"] == "getKRByQuery"
    assert result["data"]["write"] is False


def test_api_describe_missing_interface_is_actionable() -> None:
    result = discover.api_describe("NoSuchService", "nope")

    assert result["ok"] is False
    assert "目录中没有" in result["error"]["message"]
    assert "keyword" in result["error"]["hint"]


def test_api_describe_search() -> None:
    result = discover.api_describe(keyword="krService")

    assert result["ok"] is True
    assert all(hit["service"] == "KRService" for hit in result["data"])


def test_api_describe_without_arguments_explains_usage() -> None:
    result = discover.api_describe()

    assert result["ok"] is False
    assert "service + method" in result["error"]["message"]


def test_server_registers_tools() -> None:
    from mybmo_mcp.server import build_server

    server = build_server()

    # 通过 MCP 的工具管理器取清单；这是当前版本唯一的列举入口
    tools = server._tool_manager.list_tools()
    names = {tool.name for tool in tools}

    assert {"list_accounts", "account_snapshot", "keyword_plan", "api_describe"} <= names


def test_accounts_service_is_thin_reexport() -> None:
    """工具层不应自己实现业务逻辑，只转发到 services。"""
    assert accounts_tools.accounts_service is accounts_service
