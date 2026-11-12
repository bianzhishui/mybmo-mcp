from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.support.memory import MemoryAppSecretStore, MemoryCredentialStore, MemoryTenancyStore

from mybmo_mcp.auth.app_secrets import AppSecret
from mybmo_mcp.auth.binding import BindingManager, StateStore
from mybmo_mcp.auth.models import STATUS_ACTIVE
from mybmo_mcp.auth.oauth import TokenBundle
from mybmo_mcp.baidu.errors import CredentialError

KEY = b"k" * 32
CB = "https://mcp.example.com/callback"
APP = "20426c0457c842036585e268d456fe45"
TENANT = "t_default"


class FakeOAuth:
    def __init__(self, *, user_name: str = "示例账户") -> None:
        self.user_name = user_name
        self.exchange_calls = 0

    async def exchange(self, auth_code: str, user_id: int) -> TokenBundle:
        self.exchange_calls += 1
        now = time.time()
        return TokenBundle(
            access_token="at-1",
            refresh_token="rt-1",
            open_id="open-1",
            user_id=user_id,
            expires_at=now + 86400,
            refresh_expires_at=now + 2592000,
        )

    async def user_info(self, access_token: str, *, user_id: int = 0, open_id: str = ""):
        return {"masterName": self.user_name, "userId": user_id}


def build_manager(
    tmp_path: Path, oauth: FakeOAuth | None = None, *, now=time.time
) -> tuple[BindingManager, MemoryCredentialStore]:
    """绑定管理器 + 一个"单租户"上下文（file 后端，租户无实质意义）。"""
    vault = MemoryCredentialStore()
    apps = MemoryAppSecretStore()
    apps.save(AppSecret(app_id=APP, secret_key="sk-1"))
    tenancy = MemoryTenancyStore()
    ctx = SimpleNamespace(
        tenant_id=TENANT,
        store=vault,
        tenancy=tenancy,
        app_secrets=apps,
        registry=None,
        access=None,
        credentials=None,
    )
    manager = BindingManager(
        StateStore(tmp_path / "data", now=now),
        redirect_uri=CB,
        context_for=lambda _tenant: ctx,
        oauth=oauth or FakeOAuth(),
    )
    return manager, vault


def test_authorize_url_carries_one_time_nonce(tmp_path: Path) -> None:
    manager, _ = build_manager(tmp_path)

    url, state = manager.authorize_url(
        subject="sub-1", tenant=TENANT, alias="axd", app_id=APP
    )

    assert state.nonce in url
    assert f"appId={APP}" in url
    assert state.alias == "axd"


def test_state_can_only_be_consumed_once(tmp_path: Path) -> None:
    states = StateStore(tmp_path / "data")
    state = states.issue(subject="s", tenant="t", alias="axd")

    assert states.consume(state.nonce) is not None
    assert states.consume(state.nonce) is None


def test_expired_state_is_rejected(tmp_path: Path) -> None:
    clock = [1000.0]
    states = StateStore(tmp_path / "data", ttl=10, now=lambda: clock[0])
    state = states.issue(subject="s", tenant="t", alias="axd")

    clock[0] = 1020.0

    assert states.consume(state.nonce) is None


@pytest.mark.parametrize("nonce", ["", "../../etc/passwd", "short", "a" * 200])
def test_suspicious_nonce_is_rejected(tmp_path: Path, nonce: str) -> None:
    states = StateStore(tmp_path / "data")

    assert states.consume(nonce) is None


async def test_complete_with_valid_state_stores_active_credential(tmp_path: Path) -> None:
    manager, vault = build_manager(tmp_path)
    _, state = manager.authorize_url(subject="s", tenant=TENANT, alias="axd", app_id=APP)

    credential, _ = await manager.complete(auth_code="code", state=state.nonce, user_id=1001)

    assert credential.alias == "axd"
    assert credential.status == STATUS_ACTIVE
    assert credential.user_name == "示例账户"
    assert vault.load("axd").access_token == "at-1"


async def test_missing_state_is_refused(tmp_path: Path) -> None:
    """多租户下 state 是租户的唯一来源：缺失就失败，不猜（ADR 0008 D5）。"""
    manager, _ = build_manager(tmp_path)

    with pytest.raises(CredentialError, match="授权链接已失效"):
        await manager.complete(auth_code="code", state="forged", user_id=1001)


async def test_replayed_state_is_refused(tmp_path: Path) -> None:
    manager, _ = build_manager(tmp_path)
    _, state = manager.authorize_url(subject="s", tenant=TENANT, alias="axd", app_id=APP)
    await manager.complete(auth_code="code", state=state.nonce, user_id=1001)

    with pytest.raises(CredentialError, match="授权链接已失效"):
        await manager.complete(
            auth_code="code-2", state=state.nonce, user_id=1001, app_id=APP
        )


async def test_missing_master_name_fails_loudly(tmp_path: Path) -> None:
    manager, _ = build_manager(tmp_path, FakeOAuth(user_name=""))
    _, state = manager.authorize_url(subject="s", tenant=TENANT, alias="axd", app_id=APP)

    with pytest.raises(CredentialError, match="81020"):
        await manager.complete(auth_code="code", state=state.nonce, user_id=1001)
