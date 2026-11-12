"""回调处理的单测。

重点不是"能成功"，而是几条**失败路径不泄漏、不误判**：
缺授权码要报出来而不是当成功、上游失败不带授权码、state 失配要降级成 unconfirmed。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.support.memory import MemoryAppSecretStore, MemoryCredentialStore, MemoryTenancyStore

from mybmo_mcp.auth.app_secrets import AppSecret
from mybmo_mcp.auth.binding import BindingManager, StateStore
from mybmo_mcp.auth.callback import complete_binding
from mybmo_mcp.auth.models import STATUS_ACTIVE
from mybmo_mcp.auth.oauth import TokenBundle
from mybmo_mcp.baidu.errors import CredentialError

KEY = b"k" * 32
CB = "https://baiduads.pandabase.top/oauth/baidu/callback"
APP = "20426c0457c842036585e268d456fe45"
TENANT = "t_default"


class FakeOAuth:
    def __init__(self, *, user_name: str = "示例账户", fail: Exception | None = None) -> None:
        self.user_name = user_name
        self.fail = fail

    async def exchange(self, auth_code: str, user_id: int) -> TokenBundle:
        if self.fail is not None:
            raise self.fail
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


def build_manager(tmp_path: Path, oauth: FakeOAuth | None = None) -> BindingManager:
    vault = MemoryCredentialStore()
    apps = MemoryAppSecretStore()
    apps.save(AppSecret(app_id=APP, secret_key="sk-1"))
    ctx = SimpleNamespace(
        tenant_id=TENANT,
        store=vault,
        tenancy=MemoryTenancyStore(),
        app_secrets=apps,
        registry=None,
        access=None,
        credentials=None,
    )
    return BindingManager(
        StateStore(tmp_path / "data"),
        redirect_uri=CB,
        context_for=lambda _tenant: ctx,
        oauth=oauth or FakeOAuth(),
    )


async def test_success_returns_alias_and_chinese_name_only(tmp_path: Path) -> None:
    manager = build_manager(tmp_path)
    _, state = manager.authorize_url(
        subject="sub-1", tenant=TENANT, alias="main", app_id=APP
    )

    outcome = await complete_binding(
        manager, {"authCode": "code-1", "state": state.nonce, "userId": "1001"}
    )

    assert outcome.ok is True
    assert outcome.status_code == 200
    assert outcome.data == {
        "alias": "main",
        "user_name": "示例账户",
        "owner": "sub-1",
        "status": STATUS_ACTIVE,
        "expires_at": outcome.data["expires_at"],
    }
    # 令牌绝不进结果体
    assert "at-1" not in repr(outcome)
    assert "rt-1" not in repr(outcome)


@pytest.mark.parametrize("params", [{}, {"state": "x"}, {"userId": "1"}])
async def test_missing_auth_code_is_reported(
    tmp_path: Path, params: dict[str, str]
) -> None:
    outcome = await complete_binding(build_manager(tmp_path), params)

    assert outcome.ok is False
    assert outcome.status_code == 400
    assert "授权码" in outcome.message
    assert outcome.hints


async def test_error_param_from_baidu_is_reported(tmp_path: Path) -> None:
    outcome = await complete_binding(
        build_manager(tmp_path), {"error": "access_denied", "error_description": "用户拒绝"}
    )

    assert outcome.ok is False
    assert outcome.status_code == 400
    assert "用户拒绝" in outcome.message
    assert outcome.data["error"] == "access_denied"


async def test_alias_from_code_key_is_accepted(tmp_path: Path) -> None:
    """百度侧的参数名未实证，``code`` 也当授权码处理。"""
    manager = build_manager(tmp_path)
    _, state = manager.authorize_url(
        subject="s", tenant=TENANT, alias="axd", app_id=APP
    )

    outcome = await complete_binding(manager, {"code": "c-1", "state": state.nonce, "uid": "7"})

    assert outcome.ok is True
    assert outcome.data["alias"] == "axd"


async def test_overlong_auth_code_is_rejected(tmp_path: Path) -> None:
    outcome = await complete_binding(build_manager(tmp_path), {"authCode": "a" * 5000})

    assert outcome.ok is False
    assert outcome.status_code == 400
    assert "长度" in outcome.message


async def test_upstream_failure_hides_auth_code(tmp_path: Path) -> None:
    secret_code = "SECRET-CODE-123"
    manager = build_manager(
        tmp_path, FakeOAuth(fail=CredentialError("accessToken 失败 code=110 message=invalid"))
    )

    _, state = manager.authorize_url(subject="s", tenant=TENANT, alias="axd", app_id=APP)
    outcome = await complete_binding(
        manager, {"authCode": secret_code, "userId": "1", "state": state.nonce}
    )

    assert outcome.ok is False
    assert outcome.status_code == 400
    assert secret_code not in outcome.message
    assert secret_code not in repr(outcome)


async def test_missing_state_is_refused(tmp_path: Path) -> None:
    """多租户下没有 state 就不知道落哪个租户，一律失败（ADR 0008 D5）。"""
    outcome = await complete_binding(build_manager(tmp_path), {"authCode": "c", "userId": "1001"})

    assert outcome.ok is False
    assert "授权链接已失效" in outcome.message


async def test_missing_user_id_is_flagged_in_hints(tmp_path: Path) -> None:
    manager = build_manager(tmp_path)
    _, state = manager.authorize_url(
        subject="s", tenant=TENANT, alias="axd", app_id=APP
    )

    outcome = await complete_binding(manager, {"authCode": "c", "state": state.nonce})

    assert outcome.ok is True
    assert any("userId" in hint for hint in outcome.hints)


async def test_non_numeric_user_id_fails_with_actionable_hint(tmp_path: Path) -> None:
    """userId 解析不出来、又没有 state 时不做兜底：既不知道账户也不知道归属，
    编一个 acct_0 只会产出没人能认领的凭据。"""
    outcome = await complete_binding(
        build_manager(tmp_path), {"authCode": "c", "userId": "not-a-number", "appId": "app-1"}
    )

    assert outcome.ok is False
    assert "授权链接已失效" in outcome.message


async def test_missing_master_name_fails(tmp_path: Path) -> None:
    manager = build_manager(tmp_path, FakeOAuth(user_name=""))
    _, state = manager.authorize_url(subject="s", tenant=TENANT, alias="axd", app_id=APP)
    outcome = await complete_binding(
        manager, {"authCode": "c", "userId": "1", "state": state.nonce}
    )

    assert outcome.ok is False
    assert outcome.status_code == 400
    assert "81020" in outcome.message
