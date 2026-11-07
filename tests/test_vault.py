from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from mybmo_mcp.auth.models import Credential
from mybmo_mcp.auth.vault import FileVault
from mybmo_mcp.baidu.errors import CredentialError

KEY = b"k" * 32
OTHER_KEY = b"x" * 32


def make_credential(alias: str = "axd", **overrides: object) -> Credential:
    fields: dict[str, object] = {
        "alias": alias,
        "user_name": "示例账户",
        "user_id": 1000001,
        "app_id": "app-123",
        "secret_key": "secret-should-never-leak",
        "access_token": "access-token-value",
        "refresh_token": "refresh-token-value",
        "open_id": "open-123",
        "expires_at": time.time() + 3600,
        "refresh_expires_at": time.time() + 86400,
        "scope": {"tool": True},
    }
    fields.update(overrides)
    return Credential(**fields)  # type: ignore[arg-type]


def test_roundtrip_preserves_all_fields(tmp_path: Path) -> None:
    vault = FileVault(tmp_path / "data", KEY)
    original = make_credential()

    vault.save(original)
    loaded = vault.load("axd")

    assert loaded == original


def test_secrets_are_not_stored_in_plaintext(tmp_path: Path) -> None:
    vault = FileVault(tmp_path / "data", KEY)
    vault.save(make_credential())

    raw = vault.path_for("axd").read_text(encoding="utf-8")

    assert "access-token-value" not in raw
    assert "refresh-token-value" not in raw
    assert "secret-should-never-leak" not in raw


def test_file_and_directory_permissions(tmp_path: Path) -> None:
    vault = FileVault(tmp_path / "data", KEY)
    vault.save(make_credential())

    file_mode = stat.S_IMODE(os.stat(vault.path_for("axd")).st_mode)
    dir_mode = stat.S_IMODE(os.stat(vault.accounts_dir).st_mode)

    assert file_mode == 0o600
    assert dir_mode == 0o700


def test_renamed_file_is_detected_by_alias_mismatch(tmp_path: Path) -> None:
    """把 A 的文件改名成 B，先被 alias 校验拦下。"""
    vault = FileVault(tmp_path / "data", KEY)
    vault.save(make_credential("axd"))

    os.rename(vault.path_for("axd"), vault.path_for("bcd"))

    with pytest.raises(CredentialError, match="不一致"):
        vault.load("bcd")


def test_renamed_and_patched_file_still_fails_to_decrypt(tmp_path: Path) -> None:
    """连 envelope 里的 alias 一起改掉，仍会被 AAD 拦住——这是防串号的最后一道。"""
    vault = FileVault(tmp_path / "data", KEY)
    vault.save(make_credential("axd"))

    source = vault.path_for("axd")
    envelope = json.loads(source.read_text(encoding="utf-8"))
    envelope["alias"] = "bcd"
    vault.path_for("bcd").write_text(json.dumps(envelope), encoding="utf-8")
    source.unlink()

    with pytest.raises(CredentialError, match="解密失败"):
        vault.load("bcd")


def test_tampered_ciphertext_is_rejected(tmp_path: Path) -> None:
    vault = FileVault(tmp_path / "data", KEY)
    vault.save(make_credential())

    path = vault.path_for("axd")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["ciphertext"] = envelope["ciphertext"][:-4] + "AAAA"
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(CredentialError, match="解密失败"):
        vault.load("axd")


def test_wrong_master_key_is_rejected(tmp_path: Path) -> None:
    FileVault(tmp_path / "data", KEY).save(make_credential())

    with pytest.raises(CredentialError, match="解密失败"):
        FileVault(tmp_path / "data", OTHER_KEY).load("axd")


def test_broken_json_reports_path(tmp_path: Path) -> None:
    vault = FileVault(tmp_path / "data", KEY)
    vault.path_for("axd").write_text("not json", encoding="utf-8")

    with pytest.raises(CredentialError) as excinfo:
        vault.load("axd")

    assert str(vault.path_for("axd")) in str(excinfo.value)


def test_missing_account_says_so(tmp_path: Path) -> None:
    vault = FileVault(tmp_path / "data", KEY)

    with pytest.raises(CredentialError, match="账户不存在"):
        vault.load("axd")


def test_scan_separates_valid_and_broken_files(tmp_path: Path) -> None:
    vault = FileVault(tmp_path / "data", KEY)
    vault.save(make_credential("axd"))
    vault.path_for("bcd").write_text("{", encoding="utf-8")

    aliases, errors = vault.scan()

    assert aliases == ["axd"]
    assert len(errors) == 1
    assert errors[0][0].name == "bcd.json"


@pytest.mark.parametrize("alias", ["", "AXD", "_axd", "a" * 33, "../etc", "a/b", "账户"])
def test_invalid_aliases_are_rejected(tmp_path: Path, alias: str) -> None:
    vault = FileVault(tmp_path / "data", KEY)

    with pytest.raises(CredentialError, match="别名不合法"):
        vault.path_for(alias)


def test_delete_removes_file_and_clears_default(tmp_path: Path) -> None:
    vault = FileVault(tmp_path / "data", KEY)
    vault.save(make_credential("axd"))
    vault.set_default("axd")

    vault.delete("axd")

    assert not vault.path_for("axd").exists()
    assert vault.default_alias() is None


def test_default_alias_must_exist(tmp_path: Path) -> None:
    vault = FileVault(tmp_path / "data", KEY)

    with pytest.raises(CredentialError, match="不存在"):
        vault.set_default("axd")

    vault.save(make_credential("axd"))
    vault.set_default("axd")

    assert vault.default_alias() == "axd"


def test_master_key_length_is_checked(tmp_path: Path) -> None:
    with pytest.raises(CredentialError, match="32 字节"):
        FileVault(tmp_path / "data", b"short")
