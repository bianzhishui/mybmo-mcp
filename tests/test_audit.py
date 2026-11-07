"""审计：动作与结果都留痕，且绝不把令牌写进去。

重点不在"能写文件"，而在四件事：

1. 凭据片段必须被脱敏——审计日志常常被整份拷贝出去排查问题
2. 报文体不进日志，只留摘要
3. 四种结果各有对应状态，``unknown`` 不能被记成 ``failed``
4. 越权拒绝与业务失败要分开，前者请求根本没发出去
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mybmo_mcp.audit.log import (
    STATE_DENIED,
    STATE_FAILED,
    STATE_SUCCEEDED,
    STATE_UNKNOWN,
    AuditLog,
    payload_digest,
)
from mybmo_mcp.auth.tenancy import AccessDenied, Principal
from mybmo_mcp.baidu.capabilities import CapabilityRegistry
from mybmo_mcp.baidu.errors import BaiduApiError
from mybmo_mcp.tools import capability as capability_tools
from mybmo_mcp.tools.runtime import set_runtime


def read_lines(directory: Path) -> list[dict]:
    files = sorted(directory.glob("*.jsonl"))
    assert files, "没有生成审计文件"
    return [json.loads(line) for line in files[-1].read_text(encoding="utf-8").splitlines()]


# ---------- 写入与脱敏 ----------


def test_record_writes_one_json_line(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    log.record(
        sub="u_zhang",
        tenant_id="t_acme",
        account="axd",
        capability="KRService.getKRByQuery",
        permission="read",
        mutating="false",
        state=STATE_SUCCEEDED,
        call_id="abc123",
        duration_ms=42,
        payload={"query": "月季"},
    )

    lines = read_lines(tmp_path)
    assert len(lines) == 1
    row = lines[0]
    assert row["sub"] == "u_zhang"
    assert row["account"] == "axd"
    assert row["capability"] == "KRService.getKRByQuery"
    assert row["state"] == STATE_SUCCEEDED
    assert row["call_id"] == "abc123"
    assert row["duration_ms"] == 42
    assert row["payload_digest"]


def test_error_message_is_redacted(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    log.record(
        sub="u_zhang",
        tenant_id="t_acme",
        account="axd",
        capability="AccountService.getAccountInfo",
        permission="read",
        mutating="false",
        state=STATE_FAILED,
        call_id="abc123",
        error="refresh failed: refresh_token=abc123secret&x=1",
    )

    row = read_lines(tmp_path)[0]
    assert "abc123secret" not in row["error"]
    assert "***" in row["error"]


def test_payload_is_not_stored_only_its_digest(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    log.record(
        sub="u",
        tenant_id="t",
        account="axd",
        capability="KeywordService.addWord",
        permission="write",
        mutating="true",
        state=STATE_SUCCEEDED,
        call_id="c1",
        payload={"keywordTypes": [{"keyword": "月季花苗", "price": 3.5}]},
    )

    raw = read_lines(tmp_path)[0]
    assert "月季花苗" not in json.dumps(raw, ensure_ascii=False)


def test_payload_digest_is_stable_and_key_order_insensitive() -> None:
    a = payload_digest({"query": "月季", "limit": 10})
    b = payload_digest({"limit": 10, "query": "月季"})
    c = payload_digest({"query": "玫瑰", "limit": 10})

    assert a == b
    assert a != c


# ---------- 与 execute_capability 的联动 ----------


class AllowAccess:
    def resolve(
        self, principal: Principal, requested: str | None = None, *, permission: str = "read"
    ) -> SimpleNamespace:
        return SimpleNamespace(alias=requested or "axd")


class DenyAccess:
    def resolve(self, principal: Principal, requested: str | None = None, *, permission=""):
        # 与真实的 tenancy.require 一致：alias 是解析后的具体账户，不是入参原样
        raise AccessDenied(
            f"当前身份（{principal.sub}）对账户 {requested or 'axd'} 的权限不足",
            sub=principal.sub,
            alias=requested or "axd",
            required=permission,
        )


class FakeTransport:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def call(self, *args, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"data": []}


class FakeRuntime:
    def __init__(self, access=None, transport=None, audit=None) -> None:
        self.capabilities = CapabilityRegistry.load()
        self.access = access or AllowAccess()
        self.transport = transport or FakeTransport()
        self.audit = audit or AuditLog(None)
        self.principal = Principal(sub="u_zhang", tenant_id="t_acme")

    def current_principal(self) -> Principal:
        return self.principal

    def transport_for(self, alias=None, *, permission="read", **kwargs):
        return self.transport


async def run_with(tmp_path: Path, **runtime_kwargs) -> dict:
    audit = AuditLog(tmp_path)
    fake = FakeRuntime(audit=audit, **runtime_kwargs)
    set_runtime(fake)  # type: ignore[arg-type]
    try:
        return await capability_tools.execute_capability(
            "KRService.getKRByQuery", input={"query": "月季"}
        )
    finally:
        set_runtime(None)


async def test_success_is_recorded_with_call_id(tmp_path: Path) -> None:
    result = await run_with(tmp_path)

    row = read_lines(tmp_path)[0]
    assert row["state"] == STATE_SUCCEEDED
    assert row["call_id"] == result["meta"]["call_id"]
    assert row["account"] == "axd"
    assert result["meta"]["account"] == "axd"


async def test_denied_is_not_recorded_as_failed(tmp_path: Path) -> None:
    """越权时请求根本没发出去，记成 failed 会让人以为百度那边出了问题。"""
    result = await run_with(tmp_path, access=DenyAccess())

    row = read_lines(tmp_path)[0]
    assert result["ok"] is False
    assert row["state"] == STATE_DENIED
    # 没传 account 时也要记下被解析出来的具体账户，否则排查时不知道想动的是谁
    assert row["account"] == "axd"
    assert "权限不足" in row["error"]


async def test_business_failure_is_recorded_as_failed(tmp_path: Path) -> None:
    error = BaiduApiError("userName 不匹配", code=81020, outcome="failed")
    await run_with(tmp_path, transport=FakeTransport(error))

    row = read_lines(tmp_path)[0]
    assert row["state"] == STATE_FAILED
    assert row["error_code"] == 81020


async def test_timeout_is_recorded_as_unknown(tmp_path: Path) -> None:
    """unknown 是「不知道远端做没做」，重发前必须先查。记成 failed 就会诱发重发。"""
    error = BaiduApiError("请求超时，结果未知", outcome="unknown")
    await run_with(tmp_path, transport=FakeTransport(error))

    row = read_lines(tmp_path)[0]
    assert row["state"] == STATE_UNKNOWN


async def test_daily_file_name_is_a_date(tmp_path: Path) -> None:
    AuditLog(tmp_path).record(
        sub="u",
        tenant_id="t",
        account="axd",
        capability="c",
        permission="read",
        mutating="false",
        state=STATE_SUCCEEDED,
        call_id="c1",
    )

    expected = time.strftime("%Y-%m-%d", time.localtime()) + ".jsonl"
    assert (tmp_path / expected).is_file()


def test_missing_directory_is_created_with_restricted_mode(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "audit"
    AuditLog(target).record(
        sub="u",
        tenant_id="t",
        account="axd",
        capability="c",
        permission="read",
        mutating="false",
        state=STATE_SUCCEEDED,
        call_id="c1",
    )

    assert target.is_dir()
    assert oct(target.stat().st_mode)[-3:] == "700"


def test_audit_write_failure_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    """磁盘写不进去时不能把调用带崩——这是权衡，注释里写明了。"""

    def boom(*args, **kwargs):
        raise OSError("No space left on device")

    log = AuditLog(tmp_path)
    monkeypatch.setattr(Path, "open", boom)

    record = log.record(
        sub="u",
        tenant_id="t",
        account="axd",
        capability="c",
        permission="read",
        mutating="false",
        state=STATE_SUCCEEDED,
        call_id="c1",
    )

    assert record.call_id == "c1"


def test_none_directory_is_a_noop() -> None:
    assert (
        AuditLog(None)
        .record(
            sub="u",
            tenant_id="t",
            account="",
            capability="c",
            permission="read",
            mutating="false",
            state=STATE_FAILED,
            call_id="c1",
        )
        .state
        == STATE_FAILED
    )


@pytest.mark.parametrize("state", [STATE_SUCCEEDED, STATE_FAILED, STATE_UNKNOWN, STATE_DENIED])
def test_all_states_round_trip(tmp_path: Path, state: str) -> None:
    AuditLog(tmp_path).record(
        sub="u",
        tenant_id="t",
        account="axd",
        capability="c",
        permission="write",
        mutating="true",
        state=state,
        call_id="c1",
    )

    assert read_lines(tmp_path)[0]["state"] == state
