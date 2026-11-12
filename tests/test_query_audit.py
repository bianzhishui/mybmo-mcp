"""query_audit 的隔离规则。

这里不打数据库：用一个会记账的假 sink 检查"算出来的可见范围对不对"。
真实 SQL 的隔离在 tests/test_pg_audit.py 里验证。
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

from mybmo_mcp.audit.pg import PgAuditSink
from mybmo_mcp.auth.models import Credential
from mybmo_mcp.auth.tenancy import Principal, TenancyStore
from mybmo_mcp.auth.vault import FileVault
from mybmo_mcp.config import Settings
from mybmo_mcp.tools import audit as audit_tools
from mybmo_mcp.tools.runtime import Runtime, set_runtime

KEY = b"k" * 32
KEY_B64 = base64.b64encode(KEY).decode()
OWNER = "owner-sub"
OTHER = "other-sub"
STRANGER = "stranger-sub"


class RecordingSink(PgAuditSink):
    """记录 query 收到的参数，不连库。"""

    def __init__(self) -> None:
        super().__init__("postgresql://unused/none")
        self.calls: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> tuple[int, list[dict[str, Any]]]:  # type: ignore[override]
        self.calls.append(kwargs)
        return 0, []


def make_runtime(tmp_path: Path, principal: Principal) -> tuple[Runtime, RecordingSink]:
    (tmp_path / "tenancy.json").write_text(
        '{"version": 2, "users": [], "accounts": [], "grants": []}', encoding="utf-8"
    )
    settings = Settings(
        baidu_app_id="app-1",
        baidu_secret_key="sk-1",
        mybmo_data_dir=tmp_path,
        mybmo_master_key=KEY_B64,
        mybmo_auth_mode="oidc",
        mybmo_audit_backend="pg",
        mybmo_database_url="postgresql://unused/none",
    )
    runtime = Runtime(settings=settings, principal=principal)
    FileVault(tmp_path, KEY).save(Credential(alias="main", user_name="账户", access_token="at"))

    tenancy: TenancyStore = runtime.tenancy
    tenancy.register_account("main", owner=OWNER, user_name="baidu-示例账户")
    tenancy.share("main", sub=OTHER, permission="read")

    sink = RecordingSink()
    runtime.__dict__["audit"] = sink  # 绕过 cached_property 注入假 sink
    return runtime, sink


async def test_grantee_only_sees_own_rows(tmp_path: Path) -> None:
    runtime, sink = make_runtime(tmp_path, Principal(sub=OTHER, tenant_id="t_default"))
    set_runtime(runtime)
    try:
        result = await audit_tools.query_audit()
    finally:
        set_runtime(None)

    assert result["ok"] is True
    call = sink.calls[0]
    assert call["visible_subs"] == [OTHER]
    assert call["visible_aliases"] == []
    assert call["tenant_id"] == "t_default"


async def test_owner_sees_own_alias(tmp_path: Path) -> None:
    runtime, sink = make_runtime(tmp_path, Principal(sub=OWNER, tenant_id="t_default"))
    set_runtime(runtime)
    try:
        await audit_tools.query_audit(account="main")
    finally:
        set_runtime(None)

    call = sink.calls[0]
    assert call["visible_subs"] == [OWNER]
    assert call["visible_aliases"] == ["main"]


async def test_stranger_cannot_query_others_user(tmp_path: Path) -> None:
    runtime, sink = make_runtime(tmp_path, Principal(sub=OTHER, tenant_id="t_default"))
    set_runtime(runtime)
    try:
        result = await audit_tools.query_audit(user=OWNER)
    finally:
        set_runtime(None)

    assert result["ok"] is False
    assert "不能查别人的调用记录" in result["error"]["message"]
    assert sink.calls == []


async def test_stranger_cannot_query_account_without_access(tmp_path: Path) -> None:
    runtime, _sink = make_runtime(tmp_path, Principal(sub=STRANGER, tenant_id="t_default"))
    set_runtime(runtime)
    try:
        result = await audit_tools.query_audit(account="main")
    finally:
        set_runtime(None)

    assert result["ok"] is False
    assert "看不到账户 main" in result["error"]["message"]


async def test_unrestricted_sees_everything(tmp_path: Path) -> None:
    runtime, sink = make_runtime(tmp_path, Principal.local_admin())
    set_runtime(runtime)
    try:
        await audit_tools.query_audit()
    finally:
        set_runtime(None)

    call = sink.calls[0]
    assert call["visible_subs"] is None and call["visible_aliases"] is None


async def test_limit_is_clamped_and_state_validated(tmp_path: Path) -> None:
    runtime, sink = make_runtime(tmp_path, Principal(sub=OWNER, tenant_id="t_default"))
    set_runtime(runtime)
    try:
        await audit_tools.query_audit(limit=9999)
        bad = await audit_tools.query_audit(state="bogus")
    finally:
        set_runtime(None)

    assert sink.calls[0]["limit"] == 200
    assert bad["ok"] is False
    assert "state 只能是" in bad["error"]["message"]


async def test_file_backend_is_rejected(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, Principal(sub=OWNER, tenant_id="t_default"))
    runtime.__dict__["audit"] = type(
        "FakeFileSink", (), {"record": lambda self, **kw: None}
    )()
    set_runtime(runtime)
    try:
        result = await audit_tools.query_audit()
    finally:
        set_runtime(None)

    assert result["ok"] is False
    assert "MYBMO_AUDIT_BACKEND=pg" in result["error"]["message"]


def test_tool_is_registered() -> None:
    from mybmo_mcp.tools import audit as module

    assert callable(module.query_audit)
    assert time.time() > 0


def test_runtime_audit_is_accessible(tmp_path: Path) -> None:
    """回归：audit 曾被双重装饰，取属性时报 'cached_property not callable'。"""
    from mybmo_mcp.audit.log import FileAuditSink

    runtime, _ = make_runtime(tmp_path, Principal(sub=OWNER, tenant_id="t_default"))
    runtime.__dict__.pop("audit", None)  # 去掉测试注入，走真实属性
    runtime.settings.mybmo_audit_backend = "file"

    sink = runtime.audit

    assert isinstance(sink, FileAuditSink)
