"""越权防线：谁能用哪个账户。

这里的每条用例都对应一个真实的越权路径，不是覆盖率凑数。最该盯住的是三条：

1. 多用户模式下授权表缺失，绝不能"回退成人人可用"
2. 知道别人的 alias 不等于能用它
3. 不传 alias 时不能悄悄落到进程级默认账户上——那在多用户下极可能属于别人
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from mybmo_mcp.auth.access import AccountAccess
from mybmo_mcp.auth.models import Credential
from mybmo_mcp.auth.registry import AccountRegistry
from mybmo_mcp.auth.tenancy import (
    MODE_LOCAL,
    MODE_OIDC,
    PERMISSION_ADMIN,
    PERMISSION_READ,
    PERMISSION_WRITE,
    AccessDenied,
    Principal,
    TenancyError,
    TenancyStore,
    required_permission,
    satisfies,
)
from mybmo_mcp.auth.vault import FileVault
from mybmo_mcp.baidu.errors import CredentialError
from mybmo_mcp.config import Settings
from mybmo_mcp.tools.runtime import Runtime

KEY = b"k" * 32
KEY_B64 = base64.b64encode(KEY).decode()

ZHANG = Principal(sub="u_zhang", tenant_id="t_acme", display_name="张三")
LI = Principal(sub="u_li", tenant_id="t_acme", display_name="李四")


def write_tenancy(
    path: Path, grants: list[dict[str, str]], users: list[dict[str, str]] | None = None
):
    payload: dict[str, object] = {"version": 1, "grants": grants}
    if users is not None:
        payload["users"] = users
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def make_credential(alias: str) -> Credential:
    return Credential(
        alias=alias,
        user_name=f"账户-{alias}",
        user_id=1000,
        app_id="app",
        secret_key="secret",
        access_token="access-token",
        refresh_token="refresh-token",
        expires_at=time.time() + 3600,
        refresh_expires_at=time.time() + 86400,
    )


# ---------- 默认拒绝 ----------


def test_oidc_mode_without_tenancy_file_fails_closed(tmp_path: Path) -> None:
    """多用户模式下没有授权表必须报错。写成"人人可用"就是全量越权。"""
    store = TenancyStore(tmp_path / "tenancy.json", mode=MODE_OIDC)

    with pytest.raises(TenancyError, match="缺少授权表"):
        store.permission_for(ZHANG, "axd")


def test_local_mode_without_tenancy_file_is_unrestricted(tmp_path: Path) -> None:
    """stdio 单用户不要求授权表，否则默认安装就无法使用。"""
    store = TenancyStore(tmp_path / "tenancy.json", mode=MODE_LOCAL)

    assert store.permission_for(Principal.local_admin(), "axd") == PERMISSION_ADMIN


def test_unknown_sub_gets_nothing(tmp_path: Path) -> None:
    path = write_tenancy(tmp_path / "tenancy.json", [{"sub": "u_zhang", "alias": "axd"}])
    store = TenancyStore(path, mode=MODE_OIDC)

    assert store.permission_for(LI, "axd") is None
    with pytest.raises(AccessDenied, match="没有账户 axd 的授权"):
        store.require(LI, "axd", PERMISSION_READ)


# ---------- 档位 ----------


def test_permission_ordering() -> None:
    assert satisfies(PERMISSION_ADMIN, PERMISSION_WRITE)
    assert satisfies(PERMISSION_WRITE, PERMISSION_READ)
    assert not satisfies(PERMISSION_READ, PERMISSION_WRITE)
    assert not satisfies(None, PERMISSION_READ)
    assert not satisfies("owner", PERMISSION_READ)


def test_read_grant_cannot_call_write_capability(tmp_path: Path) -> None:
    path = write_tenancy(
        tmp_path / "tenancy.json", [{"sub": "u_zhang", "alias": "axd", "permission": "read"}]
    )
    store = TenancyStore(path, mode=MODE_OIDC)

    store.require(ZHANG, "axd", required_permission("false"))
    with pytest.raises(AccessDenied, match="权限是 read，本次操作需要 write"):
        store.require(ZHANG, "axd", required_permission("true"))


def test_unknown_mutating_is_treated_as_write() -> None:
    """判不准时偏严：unknown 要 write，而不是 read。"""
    assert required_permission("unknown") == PERMISSION_WRITE
    assert required_permission("true") == PERMISSION_WRITE
    assert required_permission("false") == PERMISSION_READ


def test_denied_hint_names_the_gap(tmp_path: Path) -> None:
    path = write_tenancy(
        tmp_path / "tenancy.json", [{"sub": "u_zhang", "alias": "axd", "permission": "read"}]
    )
    store = TenancyStore(path, mode=MODE_OIDC)

    with pytest.raises(AccessDenied) as excinfo:
        store.require(ZHANG, "axd", PERMISSION_WRITE)

    assert excinfo.value.hint is not None
    assert "read" in excinfo.value.hint and "write" in excinfo.value.hint


# ---------- 可见性 ----------


def test_visible_filters_aliases(tmp_path: Path) -> None:
    path = write_tenancy(
        tmp_path / "tenancy.json",
        [
            {"sub": "u_zhang", "alias": "axd"},
            {"sub": "u_li", "alias": "bcd"},
        ],
    )
    store = TenancyStore(path, mode=MODE_OIDC)

    assert store.visible(ZHANG, ["axd", "bcd", "cde"]) == ["axd"]
    assert store.visible(LI, ["axd", "bcd"]) == ["bcd"]


# ---------- 授权表本身的错误 ----------


def test_duplicate_grant_is_rejected(tmp_path: Path) -> None:
    path = write_tenancy(
        tmp_path / "tenancy.json",
        [
            {"sub": "u_zhang", "alias": "axd", "permission": "read"},
            {"sub": "u_zhang", "alias": "axd", "permission": "write"},
        ],
    )

    with pytest.raises(TenancyError, match="重复授权"):
        TenancyStore(path, mode=MODE_OIDC).permission_for(ZHANG, "axd")


def test_invalid_permission_is_rejected(tmp_path: Path) -> None:
    path = write_tenancy(
        tmp_path / "tenancy.json", [{"sub": "u_zhang", "alias": "axd", "permission": "owner"}]
    )

    with pytest.raises(TenancyError, match="permission"):
        TenancyStore(path, mode=MODE_OIDC).permission_for(ZHANG, "axd")


def test_invalid_alias_is_rejected(tmp_path: Path) -> None:
    path = write_tenancy(tmp_path / "tenancy.json", [{"sub": "u_zhang", "alias": "../etc"}])

    with pytest.raises(TenancyError, match="alias 不合法"):
        TenancyStore(path, mode=MODE_OIDC).permission_for(ZHANG, "axd")


def test_broken_json_is_reported_with_path(tmp_path: Path) -> None:
    path = tmp_path / "tenancy.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(TenancyError, match="不是合法 JSON"):
        TenancyStore(path, mode=MODE_OIDC).permission_for(ZHANG, "axd")


def test_unsupported_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "tenancy.json"
    path.write_text('{"version": 99, "grants": []}', encoding="utf-8")

    with pytest.raises(TenancyError, match="版本"):
        TenancyStore(path, mode=MODE_OIDC).permission_for(ZHANG, "axd")


# ---------- 账户取用 ----------


@pytest.fixture
def vault(tmp_path: Path) -> FileVault:
    store = FileVault(tmp_path / "data", KEY)
    store.save(make_credential("axd"))
    store.save(make_credential("bcd"))
    return store


def test_unknown_alias_and_unauthorized_alias_look_the_same(
    vault: FileVault, tmp_path: Path
) -> None:
    """两种错误必须长得一样，否则这个接口就是账户名探测器。

    回显调用方自己传来的 alias 不构成泄漏——不传它就不会出现；
    真正不能出现的是别人账户的中文名、是否可用这类只有凭据库才知道的信息。
    """
    path = write_tenancy(
        tmp_path / "tenancy.json", [{"sub": "u_zhang", "alias": "axd", "permission": "read"}]
    )
    access = AccountAccess(AccountRegistry(vault, env={}), TenancyStore(path, mode=MODE_OIDC))

    with pytest.raises(AccessDenied) as denied:
        access.resolve(ZHANG, "bcd")
    with pytest.raises(AccessDenied) as missing:
        access.resolve(ZHANG, "nope")

    # 两条消息只差在回显的 alias 上，后半段（可用列表）完全相同
    assert str(denied.value).split("；")[1] == str(missing.value).split("；")[1]
    assert "可用的只有：axd" in str(missing.value)
    # 不泄漏 bcd 的中文名
    assert "账户-bcd" not in str(denied.value)


def test_multiple_accounts_require_explicit_alias(vault: FileVault, tmp_path: Path) -> None:
    """不传 alias 时不能回落到进程级默认账户——多用户下它很可能属于别人。"""
    path = write_tenancy(
        tmp_path / "tenancy.json",
        [
            {"sub": "u_zhang", "alias": "axd"},
            {"sub": "u_zhang", "alias": "bcd"},
        ],
    )
    access = AccountAccess(AccountRegistry(vault, env={}), TenancyStore(path, mode=MODE_OIDC))

    with pytest.raises(CredentialError, match="必须显式指定 alias"):
        access.resolve(ZHANG, None)


def test_single_account_is_used_implicitly(vault: FileVault, tmp_path: Path) -> None:
    path = write_tenancy(tmp_path / "tenancy.json", [{"sub": "u_zhang", "alias": "bcd"}])
    access = AccountAccess(AccountRegistry(vault, env={}), TenancyStore(path, mode=MODE_OIDC))

    assert access.resolve(ZHANG, None).alias == "bcd"


def test_no_granted_account_is_an_error_not_empty_success(vault: FileVault, tmp_path: Path) -> None:
    path = write_tenancy(tmp_path / "tenancy.json", [{"sub": "u_li", "alias": "axd"}])
    access = AccountAccess(AccountRegistry(vault, env={}), TenancyStore(path, mode=MODE_OIDC))

    with pytest.raises(CredentialError, match="没有可用的百度账户"):
        access.resolve(ZHANG, "axd")


# ---------- 端到端：越权请求不会产生网络调用 ----------


async def test_transport_denies_before_any_network_call(tmp_path: Path) -> None:
    """真实 Runtime + 真实 BalduTransport：越权在发请求之前就被拒。"""
    settings = Settings(
        _env_file=None,
        mybmo_data_dir=tmp_path / "data",
        mybmo_master_key=KEY_B64,
        mybmo_auth_mode=MODE_OIDC,
        baidu_app_id="app",
        baidu_secret_key="secret",
    )
    settings.master_key_bytes()  # 配置本身要能过，避免把「密钥错」当成「鉴权错」
    FileVault(settings.mybmo_data_dir, settings.master_key_bytes()).save(make_credential("axd"))
    write_tenancy(
        settings.mybmo_data_dir / "tenancy.json",
        [{"sub": "u_zhang", "alias": "axd", "permission": "read"}],
    )

    runtime = Runtime(settings=settings, principal=ZHANG)
    transport = runtime.transport_for(None, permission=required_permission("true"))

    with pytest.raises(AccessDenied, match="需要 write"):
        await transport.call("CampaignService", "addCampaign", {})


async def test_missing_identity_in_oidc_mode_is_refused(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        mybmo_data_dir=tmp_path / "data",
        mybmo_master_key=KEY_B64,
        mybmo_auth_mode=MODE_OIDC,
    )
    runtime = Runtime(settings=settings)

    with pytest.raises(TenancyError, match="没有携带身份"):
        runtime.current_principal()
