"""PgCredentialStore 集成测试（ADR 0007）。

需要真实 PostgreSQL：设 ``MYBMO_TEST_DATABASE_URL`` 才会跑，否则跳过。
每个用例建一个**临时 schema**，跑 001 + 003 两份迁移，用完删掉——
互不干扰，也不碰正式数据。
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest

from mybmo_mcp.auth.models import Credential
from mybmo_mcp.auth.pg_store import PgCredentialStore
from mybmo_mcp.auth.store import CredentialStore
from mybmo_mcp.auth.vault import FileVault
from mybmo_mcp.baidu.errors import CredentialError

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = [
    (ROOT / "migrations" / "001_init.sql").read_text(encoding="utf-8"),
    (ROOT / "migrations" / "003_credentials.sql").read_text(encoding="utf-8"),
]
DSN = os.environ.get("MYBMO_TEST_DATABASE_URL", "")
KEY = b"k" * 32

pytestmark = pytest.mark.skipif(not DSN, reason="需要 MYBMO_TEST_DATABASE_URL 才能跑 PG 集成测试")


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


@pytest.fixture
def schema() -> str:
    name = f"t_{uuid.uuid4().hex[:12]}"
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {name}")
        conn.execute(f"SET search_path TO {name}")
        for sql in MIGRATIONS:
            conn.execute(sql)
    yield name
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {name} CASCADE")


@pytest.fixture
def store(schema: str) -> PgCredentialStore:
    import psycopg
    from psycopg.rows import dict_row

    def connect():
        return psycopg.connect(DSN, options=f"-c search_path={schema}", row_factory=dict_row)

    return PgCredentialStore(DSN, KEY, tenant_id="t_default", connect=connect)


def test_file_vault_satisfies_store_protocol(tmp_path: Path) -> None:
    """两个后端必须能互换——协议是 ADR 0007 D3 的前提。"""
    assert isinstance(FileVault(tmp_path / "data", KEY), CredentialStore)


def test_roundtrip_preserves_all_fields(store: PgCredentialStore) -> None:
    original = make_credential()
    store.save(original)

    assert store.load("axd") == original


def test_secrets_are_not_stored_in_plaintext(store: PgCredentialStore, schema: str) -> None:
    store.save(make_credential())
    import psycopg

    with psycopg.connect(DSN, options=f"-c search_path={schema}") as conn:
        row = conn.execute("SELECT cipher FROM credentials WHERE alias = 'axd'").fetchone()
    raw = bytes(row[0])

    assert b"access-token-value" not in raw
    assert b"refresh-token-value" not in raw
    assert b"secret-should-never-leak" not in raw


def test_plaintext_columns_are_queryable(store: PgCredentialStore, schema: str) -> None:
    """要查询的字段必须是明文列，否则授权与约束都做不了。"""
    store.save(make_credential(user_name="账户甲"))
    import psycopg

    with psycopg.connect(DSN, options=f"-c search_path={schema}") as conn:
        row = conn.execute(
            "SELECT app_id, user_name, baidu_user_id, status FROM credentials WHERE alias = 'axd'"
        ).fetchone()

    assert row == ("app-123", "账户甲", 1000001, "active")


def test_one_baidu_account_one_app(store: PgCredentialStore) -> None:
    """ADR 0006 D2：同一租户内，一个百度账户只能绑一个应用。"""
    store.save(make_credential("first", app_id="app-A"))

    with pytest.raises(CredentialError) as exc:
        store.save(make_credential("second", app_id="app-B"))

    assert "只能绑定一个应用" in str(exc.value)


def test_one_app_one_account(store: PgCredentialStore) -> None:
    store.save(make_credential("first", user_id=1))

    with pytest.raises(CredentialError) as exc:
        store.save(make_credential("second", user_id=2, app_id="app-123"))

    assert "只能服务一个账户" in str(exc.value)


def test_pending_rows_do_not_block_each_other(store: PgCredentialStore) -> None:
    """尚未拿到 userId 的 pending 行（user_id=0）不参与唯一性判定。"""
    store.save(make_credential("a", user_id=0, app_id="app-A", status="pending"))
    store.save(make_credential("b", user_id=0, app_id="app-B", status="pending"))

    assert store.scan()[0] == ["a", "b"]


def test_default_alias_is_exclusive(store: PgCredentialStore) -> None:
    store.save(make_credential("a"))
    store.save(make_credential("b", user_id=2, app_id="app-2"))
    store.set_default("a")
    assert store.default_alias() == "a"

    store.set_default("b")
    assert store.default_alias() == "b"


def test_delete_is_idempotent(store: PgCredentialStore) -> None:
    store.save(make_credential("a"))
    store.delete("a")
    store.delete("a")

    assert store.aliases() == []


def test_scan_reports_tampered_rows(store: PgCredentialStore, schema: str) -> None:
    """密文坏了不能静默消失——要能报出来。"""
    store.save(make_credential("a"))
    import psycopg

    with psycopg.connect(DSN, options=f"-c search_path={schema}") as conn:
        conn.execute("UPDATE credentials SET cipher = %s WHERE alias = 'a'", (b"\x00" * 32,))

    aliases, errors = store.scan()
    assert aliases == []
    assert len(errors) == 1
    assert "t_default/a" in str(errors[0][0])


def test_cipher_is_bound_to_tenant(store: PgCredentialStore, schema: str) -> None:
    """AAD 带租户：把行搬到别的租户下必须解不开。"""
    store.save(make_credential("a"))
    import psycopg

    with psycopg.connect(DSN, options=f"-c search_path={schema}") as conn:
        conn.execute("UPDATE credentials SET tenant_id = 't_other' WHERE alias = 'a'")

    with pytest.raises(CredentialError, match="账户不存在"):
        store.load("a")

    other = PgCredentialStore(DSN, KEY, tenant_id="t_other", connect=store.connect_override)
    with pytest.raises(CredentialError, match="解密失败"):
        other.load("a")


def test_locked_yields_a_writable_handle(store: PgCredentialStore) -> None:
    """行锁句柄必须能把新令牌写回同一事务（ADR 0007 D4）。"""
    store.save(make_credential("a"))

    with store.locked("a") as row:
        current = row.credential
        assert current.access_token == "access-token-value"
        row.save(current.with_tokens(access_token="at-new"))

    assert store.load("a").access_token == "at-new"


def test_locked_rejects_unknown_alias(store: PgCredentialStore) -> None:
    with pytest.raises(CredentialError, match="账户不存在"):
        with store.locked("nope"):
            pass
