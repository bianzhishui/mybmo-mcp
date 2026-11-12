"""账户分享：只有 owner 能分享，且只能给 read / write。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from tests.support.memory import (
    attach_memory_context,
)

from mybmo_mcp.auth.models import Credential
from mybmo_mcp.auth.tenancy import Principal
from mybmo_mcp.config import Settings
from mybmo_mcp.tools import accounts as accounts_tools
from mybmo_mcp.tools.runtime import Runtime, set_runtime

KEY = b"k" * 32
KEY_B64 = base64.b64encode(KEY).decode()
OWNER = Principal(sub="owner-sub", tenant_id="t_default")
OTHER = Principal(sub="other-sub", tenant_id="t_default")


def write_tenancy(root: Path) -> None:
    (root / "tenancy.json").write_text(
        json.dumps({"version": 2, "users": [], "accounts": [], "grants": []}), encoding="utf-8"
    )


def make_runtime(tmp_path: Path, principal: Principal) -> Runtime:
    write_tenancy(tmp_path)
    settings = Settings(
        baidu_app_id="app-1", baidu_secret_key="sk-1", mybmo_data_dir=tmp_path,
        mybmo_master_key=KEY_B64, mybmo_auth_mode="oidc",
        mybmo_baidu_callback_url="https://x/oauth/baidu/callback",
    )
    rt = Runtime(settings=settings, principal=principal)
    ctx0 = attach_memory_context(rt, principal.tenant_id or "local")
    ctx0.store.save(Credential(alias="main", user_name="账户", access_token="at"))
    ctx0.tenancy.register_account("main", owner=OWNER.sub, user_name="账户")
    return rt


@pytest.fixture
def owner_runtime(tmp_path: Path):
    rt = make_runtime(tmp_path, OWNER)
    set_runtime(rt)
    yield rt
    set_runtime(None)


async def test_owner_can_share_read(owner_runtime: Runtime) -> None:
    result = await accounts_tools.share_account("main", "other-sub", "read")

    assert result["ok"] is True
    assert result["data"]["shares"] == {"other-sub": "read"}
    tenancy = owner_runtime.context_for_tenant(OWNER.tenant_id).tenancy
    assert tenancy.permission_for(Principal(sub="other-sub"), "main") == "read"


async def test_share_admin_is_rejected(owner_runtime: Runtime) -> None:
    result = await accounts_tools.share_account("main", "other-sub", "admin")

    assert result["ok"] is False
    assert "admin 是 owner" in result["error"]["message"]


async def test_non_owner_cannot_share(tmp_path: Path) -> None:
    rt = make_runtime(tmp_path, OTHER)
    set_runtime(rt)
    try:
        result = await accounts_tools.share_account("main", "someone", "read")
    finally:
        set_runtime(None)

    assert result["ok"] is False
    assert "只有账户 owner 能分享" in result["error"]["message"]


async def test_revoke_removes_share(owner_runtime: Runtime) -> None:
    await accounts_tools.share_account("main", "other-sub", "write")

    result = await accounts_tools.revoke_share("main", "other-sub")

    assert result["ok"] is True
    assert result["data"]["revoked"] is True
    tenancy = owner_runtime.context_for_tenant(OWNER.tenant_id).tenancy
    assert tenancy.permission_for(Principal(sub="other-sub"), "main") is None


async def test_list_shares_shows_owner_and_grants(owner_runtime: Runtime) -> None:
    await accounts_tools.share_account("main", "a-sub", "read")
    await accounts_tools.share_account("main", "b-sub", "write")

    result = await accounts_tools.list_shares("main")

    assert result["data"]["owner"] == "owner-sub"
    assert result["data"]["shares"] == {"a-sub": "read", "b-sub": "write"}
