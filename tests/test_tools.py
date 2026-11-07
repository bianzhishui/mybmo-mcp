from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest

from mybmo_mcp.auth.models import STATUS_ACTIVE, STATUS_REAUTH_REQUIRED
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
    def __init__(self, entries: list[FakeEntry] | None = None) -> None:
        self.registry = FakeRegistry(entries or [FakeEntry()])


@pytest.fixture
def runtime() -> Iterator[FakeRuntime]:
    fake = FakeRuntime()
    set_runtime(fake)  # type: ignore[arg-type]
    yield fake
    set_runtime(None)


async def test_list_accounts_returns_envelope(runtime: FakeRuntime) -> None:
    result = await accounts_tools.list_accounts()

    assert result["ok"] is True
    assert result["data"][0]["alias"] == "axd"


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
    assert "未知账户" in result["error"]["message"]


def test_server_registers_account_tools() -> None:
    from mybmo_mcp.server import build_server

    tools = build_server()._tool_manager.list_tools()
    names = {tool.name for tool in tools}

    assert names == {"list_accounts", "auth_status"}
