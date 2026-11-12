"""审计：动作与结果都留痕，且绝不把令牌写进去。

重点不在"能写文件"，而在四件事：

1. 凭据片段必须被脱敏——审计日志常常被整份拷贝出去排查问题
2. 报文体不进日志，只留摘要
3. 四种结果各有对应状态，``unknown`` 不能被记成 ``failed``
4. 越权拒绝与业务失败要分开，前者请求根本没发出去
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from tests.support.memory import MemoryAuditSink

from mybmo_mcp.audit.base import (
    STATE_DENIED,
    STATE_FAILED,
    STATE_SUCCEEDED,
    STATE_UNKNOWN,
    payload_digest,
)
from mybmo_mcp.auth.tenancy import AccessDenied, Principal
from mybmo_mcp.baidu.capabilities import CapabilityRegistry
from mybmo_mcp.baidu.errors import BaiduApiError
from mybmo_mcp.tools import capability as capability_tools
from mybmo_mcp.tools.runtime import set_runtime


def read_lines(sink: MemoryAuditSink) -> list[dict]:
    assert sink.records, "没有写入任何审计记录"
    return sink.rows()


# ---------- 写入与脱敏 ----------


def test_record_writes_one_entry() -> None:
    log = MemoryAuditSink()
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

    lines = read_lines(log)
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
    log = MemoryAuditSink()
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

    row = read_lines(log)[0]
    assert "abc123secret" not in row["error"]
    assert "***" in row["error"]


def test_payload_is_not_stored_only_its_digest(tmp_path: Path) -> None:
    log = MemoryAuditSink()
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

    raw = read_lines(log)[0]
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
        self.audit = audit or MemoryAuditSink()
        self.principal = Principal(sub="u_zhang", tenant_id="t_acme")

    def current_principal(self) -> Principal:
        return self.principal

    def context(self, principal: Principal | None = None):
        target = principal or self.current_principal()
        return SimpleNamespace(
            tenant_id=target.tenant_id or "local",
            store=None,
            tenancy=getattr(self, "tenancy", None),
            app_secrets=None,
            registry=None,
            access=self.access,
            credentials=None,
        )

    def transport_for(self, alias=None, *, permission="read", **kwargs):
        return self.transport


async def run_with(tmp_path: Path, **runtime_kwargs) -> tuple[dict, MemoryAuditSink]:
    audit = MemoryAuditSink()
    fake = FakeRuntime(audit=audit, **runtime_kwargs)
    set_runtime(fake)  # type: ignore[arg-type]
    try:
        result = await capability_tools.execute_capability(
            "KRService.getKRByQuery", input={"query": "月季"}
        )
        return result, audit
    finally:
        set_runtime(None)


async def test_success_is_recorded_with_call_id(tmp_path: Path) -> None:
    result, log = await run_with(tmp_path)

    row = read_lines(log)[0]
    assert row["state"] == STATE_SUCCEEDED
    assert row["call_id"] == result["meta"]["call_id"]
    assert row["account"] == "axd"
    assert result["meta"]["account"] == "axd"


async def test_denied_is_not_recorded_as_failed(tmp_path: Path) -> None:
    """越权时请求根本没发出去，记成 failed 会让人以为百度那边出了问题。"""
    result, log = await run_with(tmp_path, access=DenyAccess())

    row = read_lines(log)[0]
    assert result["ok"] is False
    assert row["state"] == STATE_DENIED
    # 没传 account 时也要记下被解析出来的具体账户，否则排查时不知道想动的是谁
    assert row["account"] == "axd"
    assert "权限不足" in row["error"]


async def test_business_failure_is_recorded_as_failed(tmp_path: Path) -> None:
    error = BaiduApiError("userName 不匹配", code=81020, outcome="failed")
    _, log = await run_with(tmp_path, transport=FakeTransport(error))

    row = read_lines(log)[0]
    assert row["state"] == STATE_FAILED
    assert row["error_code"] == 81020


async def test_timeout_is_recorded_as_unknown(tmp_path: Path) -> None:
    """unknown 是「不知道远端做没做」，重发前必须先查。记成 failed 就会诱发重发。"""
    error = BaiduApiError("请求超时，结果未知", outcome="unknown")
    _, log = await run_with(tmp_path, transport=FakeTransport(error))

    row = read_lines(log)[0]
    assert row["state"] == STATE_UNKNOWN
