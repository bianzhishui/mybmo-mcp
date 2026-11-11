from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mybmo_mcp.auth.access import AccountAccess
from mybmo_mcp.auth.models import STATUS_ACTIVE, STATUS_REAUTH_REQUIRED
from mybmo_mcp.auth.tenancy import MODE_LOCAL, Principal, TenancyStore
from mybmo_mcp.tools import accounts as accounts_tools
from mybmo_mcp.tools.runtime import set_runtime


class FakeEntry:
    def __init__(self, alias: str = "axd", status: str = STATUS_ACTIVE) -> None:
        self.alias = alias
        self.status = status
        self.expires_at = time.time() + 3600

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "user_name": "示例账户",
            "usable": True,
            "is_default": True,
            "status": self.status,
            "source": "file",
            "expires_at": self.expires_at,
            "note": "",
        }


class FakeRegistry:
    def __init__(self, entries: list[FakeEntry]) -> None:
        self._entries = entries

    def list_accounts(self) -> list[FakeEntry]:
        return self._entries


class FakeRuntime:
    """只替换凭据库，授权层用真实现——越权判定必须被真实走到。"""

    def __init__(self, entries: list[FakeEntry] | None = None, tenancy: TenancyStore | None = None):
        self.registry = FakeRegistry(entries or [FakeEntry()])
        self.tenancy = tenancy or TenancyStore(Path("/nonexistent/tenancy.json"), mode=MODE_LOCAL)
        self.access = AccountAccess(self.registry, self.tenancy)  # type: ignore[arg-type]
        self.principal: Principal | None = None

    def current_principal(self) -> Principal:
        return self.principal or Principal.local_admin()


@pytest.fixture
def runtime() -> Iterator[FakeRuntime]:
    fake = FakeRuntime()
    set_runtime(fake)  # type: ignore[arg-type]
    yield fake
    set_runtime(None)


async def test_list_accounts_returns_envelope(runtime: FakeRuntime) -> None:
    result = await accounts_tools.list_accounts()

    assert result["ok"] is True
    assert result["data"]["accounts"][0]["alias"] == "axd"
    assert result["data"]["identity"]["sub"] == "local"


async def test_auth_status_reports_remaining_time(runtime: FakeRuntime) -> None:
    result = await accounts_tools.auth_status()

    assert result["ok"] is True
    assert result["data"][0]["needs_reauth"] is False
    assert result["data"][0]["expires_in_seconds"] > 3500


async def test_auth_status_flags_reauth() -> None:
    set_runtime(FakeRuntime([FakeEntry(status=STATUS_REAUTH_REQUIRED)]))  # type: ignore[arg-type]
    try:
        result = await accounts_tools.auth_status("axd")
    finally:
        set_runtime(None)

    assert result["data"][0]["needs_reauth"] is True


async def test_auth_status_rejects_unknown_alias(runtime: FakeRuntime) -> None:
    result = await accounts_tools.auth_status("nothing")

    assert result["ok"] is False
    # 不区分「不存在」与「无权」：两者给同一条错误，避免这个工具变成账户名探测器
    assert "不能使用账户 nothing" in result["error"]["message"]
    assert "axd" in result["error"]["message"]


async def test_local_mode_without_tenancy_file_sees_everything() -> None:
    """stdio 单用户不要求 tenancy.json，否则默认安装就跑不起来。"""
    set_runtime(FakeRuntime([FakeEntry("axd"), FakeEntry("bcd")]))  # type: ignore[arg-type]
    try:
        result = await accounts_tools.list_accounts()
    finally:
        set_runtime(None)

    assert result["data"]["count"] == 2
    assert result["data"]["identity"]["unrestricted"] is True


def test_server_registers_all_tools() -> None:
    from mybmo_mcp.server import build_server

    tools = build_server()._tool_manager.list_tools()
    names = {tool.name for tool in tools}

    assert names == {
        "list_accounts",
        "auth_status",
        "bind_account",
        "search_capabilities",
        "get_capability_schema",
        "execute_capability",
    }
