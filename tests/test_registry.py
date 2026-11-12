from __future__ import annotations

import time
from pathlib import Path

import pytest

from mybmo_mcp.auth.models import Credential
from mybmo_mcp.auth.registry import AccountRegistry, credential_from_env
from mybmo_mcp.auth.vault import FileVault
from mybmo_mcp.baidu.errors import CredentialError

KEY = b"k" * 32


def credential(alias: str, *, user_name: str = "示例账户", token: str = "tok") -> Credential:
    return Credential(
        alias=alias,
        user_name=user_name,
        access_token=token,
        expires_at=time.time() + 3600,
    )


@pytest.fixture
def vault(tmp_path: Path) -> FileVault:
    return FileVault(tmp_path / "data", KEY)


def test_lists_file_accounts_with_default_marked(vault: FileVault) -> None:
    vault.save(credential("axd", user_name="账户甲"))
    vault.save(credential("wds02", user_name="账户乙"))
    vault.set_default("wds02")

    entries = {entry.alias: entry for entry in AccountRegistry(vault, env={}).list_accounts()}

    assert set(entries) == {"axd", "wds02"}
    assert entries["wds02"].is_default is True
    assert entries["axd"].is_default is False
    assert entries["axd"].usable is True
    assert entries["axd"].source == "file"
    assert entries["axd"].user_name == "账户甲"


def test_environment_credential_appears_but_is_not_default(vault: FileVault) -> None:
    env = {"BAIDU_USER_NAME": "临时账户", "BAIDU_ACCESS_TOKEN": "env-token"}

    entries = AccountRegistry(vault, env=env).list_accounts()

    assert len(entries) == 1
    assert entries[0].alias == "env"
    assert entries[0].source == "environment"
    assert entries[0].is_default is False


def test_environment_credential_can_be_fetched_by_alias(vault: FileVault) -> None:
    registry = AccountRegistry(
        vault, env={"BAIDU_USER_NAME": "临时账户", "BAIDU_ACCESS_TOKEN": "t"}
    )

    assert registry.get("env").access_token == "t"


def test_corrupted_file_fails_loudly_with_path(vault: FileVault) -> None:
    vault.save(credential("axd"))
    broken = vault.path_for("wds02")
    broken.write_text("{}", encoding="utf-8")

    with pytest.raises(CredentialError) as excinfo:
        AccountRegistry(vault, env={}).list_accounts()

    message = str(excinfo.value)
    assert "无法读取" in message
    assert str(broken) in message


def test_resolve_prefers_default_then_env_then_single(vault: FileVault) -> None:
    vault.save(credential("axd", token="a"))
    vault.save(credential("wds02", token="b"))
    registry = AccountRegistry(vault, env={})

    with pytest.raises(CredentialError, match="必须显式指定 alias"):
        registry.resolve()

    vault.set_default("wds02")
    assert registry.resolve().access_token == "b"

    env_registry = AccountRegistry(
        vault, env={"BAIDU_USER_NAME": "临时", "BAIDU_ACCESS_TOKEN": "e"}
    )
    assert env_registry.resolve("env").access_token == "e"


def test_resolve_single_account_without_default(vault: FileVault) -> None:
    vault.save(credential("axd", token="only"))

    assert AccountRegistry(vault, env={}).resolve().access_token == "only"


def test_resolve_without_any_account_gives_actionable_error(vault: FileVault) -> None:
    with pytest.raises(CredentialError, match="没有任何已绑定的百度账户"):
        AccountRegistry(vault, env={}).resolve()


def test_credential_from_env_requires_name_and_token() -> None:
    assert credential_from_env({}) is None
    assert credential_from_env({"BAIDU_USER_NAME": "只有名字"}) is None

    parsed = credential_from_env(
        {
            "BAIDU_USER_NAME": "账户",
            "BAIDU_ACCESS_TOKEN": "tok",
            "BAIDU_USER_ID": "42",
            "BAIDU_EXPIRES_AT": "1700000000",
        }
    )
    assert parsed is not None
    assert parsed.alias == "env"
    assert parsed.user_id == 42
    assert parsed.expires_at == 1700000000.0


def test_credential_from_env_rejects_bad_numbers() -> None:
    with pytest.raises(CredentialError, match="BAIDU_USER_ID"):
        credential_from_env(
            {"BAIDU_USER_NAME": "x", "BAIDU_ACCESS_TOKEN": "y", "BAIDU_USER_ID": "abc"}
        )


def test_public_view_contains_no_tokens(vault: FileVault) -> None:
    vault.save(credential("axd", token="secret-token"))

    rendered = vault.load("axd").to_public()

    assert "secret-token" not in str(rendered)
    assert set(rendered) == {
        "alias",
        "user_name",
        "usable",
        "status",
        "expires_at",
        "refresh_expires_at",
        "note",
    }


def test_pending_placeholder_is_hidden_from_accounts(tmp_path: Path) -> None:
    """ADR 0006 D4：预登记但未完成授权的占位凭据不能出现在可选项里。"""
    store = FileVault(tmp_path / "data", KEY)
    store.save(credential("ready"))
    store.save(
        Credential(alias="waiting", app_id="app-1", secret_key="sk-1", status="pending")
    )
    registry = AccountRegistry(store, env={})

    assert [entry.alias for entry in registry.list_accounts()] == ["ready"]
    # 只剩一个可用账户时，resolve() 不该被占位记录干扰
    assert registry.resolve().alias == "ready"
