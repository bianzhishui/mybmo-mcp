"""加解密统一入口的护栏。

最重要的是第一条：**AAD 的字节形态被钉死**。它一变，所有已加密的凭据都解不开
（文件后端与 PG 里躺着的都是按这些前缀加密的），所以这里逐字节断言，
而不是"能解密就行"。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from mybmo_mcp.auth import app_secrets, crypto, pg_store
from mybmo_mcp.baidu.errors import CredentialError

KEY = b"k" * 32


def test_aad_prefixes_are_frozen() -> None:
    """改了这两个字符串等于让线上全部密文失效。"""
    assert pg_store._aad("t_default", "main") == b"mybmo-mcp/credential/v2/t_default/main"
    assert app_secrets._aad("20426c04") == b"mybmo-mcp/app/v1/20426c04"


def test_seal_and_open_roundtrip() -> None:
    a = crypto.aead(KEY)
    aad = crypto.aad("mybmo-mcp/test/v1/", "x")

    nonce, cipher = crypto.seal(a, {"hello": "世界"}, aad)

    assert crypto.open_json(a, nonce, cipher, aad, label="测试") == {"hello": "世界"}


def test_wrong_aad_fails_loudly() -> None:
    a = crypto.aead(KEY)
    nonce, cipher = crypto.seal(a, {"k": "v"}, crypto.aad("p/", "a"))

    with pytest.raises(CredentialError, match="解密失败"):
        crypto.open_json(a, nonce, cipher, crypto.aad("p/", "b"), label="测试")


def test_wrong_key_fails_loudly() -> None:
    nonce, cipher = crypto.seal(crypto.aead(KEY), {"k": "v"}, b"aad")

    with pytest.raises(CredentialError, match="解密失败"):
        crypto.open_json(crypto.aead(b"x" * 32), nonce, cipher, b"aad", label="测试")


def test_master_key_length_is_enforced() -> None:
    with pytest.raises(CredentialError, match="主密钥必须是"):
        crypto.aead(b"short")


def test_atomic_write_sets_private_mode(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    crypto.atomic_write(path, b'{"a":1}')

    assert path.read_bytes() == b'{"a":1}'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # 不留临时文件
    assert [p.name for p in tmp_path.iterdir()] == ["f.json"]


def test_atomic_write_cleans_up_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError):
        crypto.atomic_write(tmp_path / "f.json", b"x")

    assert list(tmp_path.iterdir()) == []
