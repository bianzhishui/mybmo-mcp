"""自助绑定：用户调 bind_account 拿授权链接，自己在浏览器完成授权。

这组用例守两件事：
1. 链接里的 state 绑的是**调用者本人**——绑完 owner 就是他，不需要运维介入
2. 不能借绑定抢别人的账户别名（也不能绑一个没有归属记录的孤儿凭据）
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from tests.support.memory import (
    attach_memory_context,
)

from mybmo_mcp.auth.app_secrets import AppSecret
from mybmo_mcp.auth.models import Credential
from mybmo_mcp.auth.tenancy import Principal
from mybmo_mcp.config import Settings
from mybmo_mcp.tools import accounts as accounts_tools
from mybmo_mcp.tools.runtime import Runtime, set_runtime

KEY = b"k" * 32
#: 形如真实百度 appId（32 位十六进制），app_secrets 会校验格式
APP_ID = "20426c0457c842036585e268d456fe45"
KEY_B64 = base64.b64encode(KEY).decode()
CALLBACK = "https://baiduads.pandabase.top/oauth/baidu/callback"
ME = Principal(sub="alice-sub", tenant_id="t_default")


def write_empty_tenancy(root: Path) -> None:
    """oidc 模式要求授权表存在（缺了就是硬错误，这是刻意的）。"""
    (root / "tenancy.json").write_text(
        json.dumps({"version": 2, "users": [], "accounts": [], "grants": []}),
        encoding="utf-8",
    )


@pytest.fixture
def runtime(tmp_path: Path):
    settings = Settings(
        baidu_app_id=APP_ID,
        baidu_secret_key="sk-1",
        mybmo_data_dir=tmp_path,
        mybmo_master_key=KEY_B64,
        mybmo_auth_mode="oidc",
        mybmo_baidu_callback_url=CALLBACK,
    )
    rt = Runtime(settings=settings, principal=ME)
    ctx = attach_memory_context(rt, ME.tenant_id or "local")
    # ADR 0006 D3：绑定用的应用密钥必须在服务器上预登记（这里模拟 CLI 已登记）
    ctx.app_secrets.save(AppSecret(app_id=APP_ID, secret_key="sk-1"))
    set_runtime(rt)
    yield rt
    set_runtime(None)


def seed_account(runtime: Runtime, alias: str, *, owner: str | None) -> None:
    ctx = runtime.context_for_tenant(ME.tenant_id or "local")
    ctx.store.save(Credential(alias=alias, user_name=f"账户-{alias}", access_token="at"))
    if owner is not None:
        ctx.tenancy.register_account(alias, owner=owner, user_name=f"账户-{alias}")


async def test_bind_account_returns_url_bound_to_caller(runtime: Runtime) -> None:
    result = await accounts_tools.bind_account("mine", APP_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["alias"] == "mine"
    assert data["owner"] == ME.sub
    assert data["expires_in_seconds"] > 0
    assert CALLBACK.split("://")[1] in data["authorize_url"].replace("%2F", "/")
    assert "state=" in data["authorize_url"]
    # state 落盘，等回调时消费
    assert (runtime.settings.mybmo_data_dir / "binding_state").is_dir()


async def test_bind_account_rejects_alias_owned_by_someone_else(runtime: Runtime) -> None:
    seed_account(runtime, "shared", owner="bob-sub")

    result = await accounts_tools.bind_account("shared", APP_ID)

    assert result["ok"] is False
    assert "已属于别人" in result["error"]["message"]
    assert "bob-sub" in result["error"]["message"]


async def test_bind_account_rejects_orphan_credential(runtime: Runtime) -> None:
    """凭据在库里但没有归属记录——绑了也不可见，先让负责人补 accounts。"""
    seed_account(runtime, "orphan", owner=None)

    result = await accounts_tools.bind_account("orphan", APP_ID)

    assert result["ok"] is False
    assert "归属记录" in result["error"]["message"]


async def test_bind_account_allows_rebinding_own_account(runtime: Runtime) -> None:
    seed_account(runtime, "mine", owner=ME.sub)

    result = await accounts_tools.bind_account("mine", APP_ID)

    assert result["ok"] is True
    assert result["data"]["owner"] == ME.sub


async def test_bind_account_without_callback_config_fails_clearly(tmp_path: Path) -> None:
    settings = Settings(
        baidu_app_id=APP_ID,
        baidu_secret_key="sk-1",
        mybmo_data_dir=tmp_path,
        mybmo_master_key=KEY_B64,
        mybmo_auth_mode="oidc",
        mybmo_baidu_callback_url="",
    )
    rt = Runtime(settings=settings, principal=ME)
    ctx = attach_memory_context(rt, ME.tenant_id or "local")
    ctx.app_secrets.save(AppSecret(app_id=APP_ID, secret_key="sk-1"))
    set_runtime(rt)
    try:
        result = await accounts_tools.bind_account("mine", APP_ID)
    finally:
        set_runtime(None)

    assert result["ok"] is False
    assert "MYBMO_BAIDU_CALLBACK_URL" in result["error"]["message"]


async def test_list_accounts_shows_ownership(runtime: Runtime) -> None:
    seed_account(runtime, "mine", owner=ME.sub)
    seed_account(runtime, "shared", owner="bob-sub")
    runtime.context_for_tenant(ME.tenant_id or "local").tenancy.share(
        "shared", sub=ME.sub, permission="write"
    )

    result = await accounts_tools.list_accounts()

    rows = {row["alias"]: row for row in result["data"]["accounts"]}
    assert set(rows) == {"mine", "shared"}
    assert rows["mine"]["is_owner"] is True
    assert rows["mine"]["my_permission"] == "admin"
    assert rows["shared"]["is_owner"] is False
    assert rows["shared"]["owner"] == "bob-sub"
    assert rows["shared"]["my_permission"] == "write"
    assert result["data"]["identity"]["account_model"] == "owned"
