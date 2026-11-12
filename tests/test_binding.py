from __future__ import annotations

import time
from pathlib import Path

import pytest

from mybmo_mcp.auth.binding import BindingManager, StateStore
from mybmo_mcp.auth.models import STATUS_ACTIVE, STATUS_UNCONFIRMED
from mybmo_mcp.auth.oauth import TokenBundle
from mybmo_mcp.auth.vault import FileVault
from mybmo_mcp.baidu.errors import CredentialError

KEY = b"k" * 32
CB = "https://mcp.example.com/callback"


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
) -> tuple[BindingManager, FileVault]:
    vault = FileVault(tmp_path / "data", KEY)
    states = StateStore(tmp_path / "data", now=now)
    manager = BindingManager(
        oauth or FakeOAuth(),
        vault,
        states,
        app_id="app-1",
        secret_key="sk-1",
        redirect_uri=CB,
    )
    return manager, vault


def test_authorize_url_carries_one_time_nonce(tmp_path: Path) -> None:
    manager, _ = build_manager(tmp_path)

    url, state = manager.authorize_url(subject="sub-1", tenant="t1", alias="axd")

    assert state.nonce in url
    assert "appId=app-1" in url
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
    _, state = manager.authorize_url(subject="s", tenant="t", alias="axd")

    credential, _ = await manager.complete(auth_code="code", state=state.nonce, user_id=1001)

    assert credential.alias == "axd"
    assert credential.status == STATUS_ACTIVE
    assert credential.user_name == "示例账户"
    assert vault.load("axd").access_token == "at-1"


async def test_complete_without_state_falls_back_and_marks_unconfirmed(tmp_path: Path) -> None:
    manager, vault = build_manager(tmp_path)

    credential, _ = await manager.complete(auth_code="code", state="forged", user_id=1001)

    assert credential.alias == "acct_1001"
    assert credential.status == STATUS_UNCONFIRMED
    assert "待人工确认" in vault.load("acct_1001").note


async def test_replayed_state_is_refused_instead_of_duplicating(tmp_path: Path) -> None:
    """state 已被消费 → 走 appId 兜底；但同一百度账户已经绑过，必须拒绝。

    这比"降级成 acct_<userId> 的 unconfirmed 副本"好：副本会让同一账户出现
    两套并行令牌，且没人认领（ADR 0006 D6）。
    """
    manager, _ = build_manager(tmp_path)
    _, state = manager.authorize_url(subject="s", tenant="t", alias="axd")
    await manager.complete(auth_code="code", state=state.nonce, user_id=1001)

    with pytest.raises(CredentialError, match="只能绑定一个应用"):
        await manager.complete(
            auth_code="code-2", state=state.nonce, user_id=1001, app_id="app-1"
        )


async def test_missing_master_name_fails_loudly(tmp_path: Path) -> None:
    manager, _ = build_manager(tmp_path, FakeOAuth(user_name=""))

    with pytest.raises(CredentialError, match="81020"):
        await manager.complete(auth_code="code", state="", user_id=1001)
