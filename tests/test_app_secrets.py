"""应用密钥登记（app_secrets）测试。

两个后端都要满足同一件事：密钥能按 app_id 存回来，但**永远不以明文出现**，
且密文绑死 app_id——把 A 的密文挂到 B 名下必须解不开。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from mybmo_mcp.auth.app_secrets import (
    AAD_PREFIX,
    AppSecret,
    AppSecretStore,
    FileAppSecretStore,
    PgAppSecretStore,
    validate_app_id,
)
from mybmo_mcp.baidu.errors import CredentialError

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = [
    (ROOT / "migrations" / "001_init.sql").read_text(encoding="utf-8"),
    (ROOT / "migrations" / "003_credentials.sql").read_text(encoding="utf-8"),
    (ROOT / "migrations" / "005_tenant_client.sql").read_text(encoding="utf-8"),
]
DSN = os.environ.get("MYBMO_TEST_DATABASE_URL", "")
KEY = b"k" * 32
APP_A = "20426c0457c842036585e268d456fe45"
APP_B = "d83aa97522af9aa92a59892993085c80"
SECRET = "5dddca5c6643ce275b0ea6a96168f6ac"


def test_file_store_satisfies_protocol(tmp_path: Path) -> None:
    assert isinstance(FileAppSecretStore(tmp_path, KEY), AppSecretStore)


def test_app_id_format_is_enforced() -> None:
    with pytest.raises(CredentialError, match="不合法"):
        validate_app_id("app-1")


def test_file_roundtrip_and_list(tmp_path: Path) -> None:
    store = FileAppSecretStore(tmp_path, KEY)
    store.save(AppSecret(app_id=APP_A, secret_key=SECRET, scope="basic", note="主应用"))

    loaded = store.get(APP_A)
    assert loaded is not None
    assert loaded.secret_key == SECRET
    assert loaded.scope == "basic"
    assert [app.app_id for app in store.list()] == [APP_A]


def test_file_secret_is_encrypted(tmp_path: Path) -> None:
    store = FileAppSecretStore(tmp_path, KEY)
    store.save(AppSecret(app_id=APP_A, secret_key=SECRET))

    raw = store.path_for(APP_A).read_text(encoding="utf-8")
    assert SECRET not in raw
    assert "secret_key" not in raw


def test_file_repr_does_not_leak_secret() -> None:
    assert SECRET not in repr(AppSecret(app_id=APP_A, secret_key=SECRET))
    assert "secret_key" not in AppSecret(app_id=APP_A, secret_key=SECRET).to_public()


def test_file_cipher_bound_to_app_id(tmp_path: Path) -> None:
    """把 A 的密文文件改名成 B，必须解不开——AAD 绑的正是 app_id。"""
    store = FileAppSecretStore(tmp_path, KEY)
    store.save(AppSecret(app_id=APP_A, secret_key=SECRET))
    store.path_for(APP_A).rename(store.path_for(APP_B))

    with pytest.raises(CredentialError, match="解密失败"):
        store.get(APP_B)


def test_file_delete(tmp_path: Path) -> None:
    store = FileAppSecretStore(tmp_path, KEY)
    store.save(AppSecret(app_id=APP_A, secret_key=SECRET))
    store.delete(APP_A)
    store.delete(APP_A)

    assert store.get(APP_A) is None


def test_aad_prefix_is_stable() -> None:
    """AAD 前缀改动等于让所有已登记的密钥失效，钉住它。"""
    assert AAD_PREFIX == "mybmo-mcp/app/v1/"


# ---------------------------------------------------------------- PG 后端

pg_only = pytest.mark.skipif(not DSN, reason="需要 MYBMO_TEST_DATABASE_URL 才能跑 PG 集成测试")


@pytest.fixture
def pg_schema() -> str:
    import psycopg

    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET search_path TO {schema}")
        for sql in MIGRATIONS:
            conn.execute(sql)
    yield schema
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def pg_store(pg_schema: str) -> PgAppSecretStore:
    import psycopg
    from psycopg.rows import dict_row

    def connect():
        return psycopg.connect(DSN, options=f"-c search_path={pg_schema}", row_factory=dict_row)

    return PgAppSecretStore(DSN, KEY, tenant_id="t_default", connect=connect)


@pg_only
def test_pg_roundtrip(pg_store: PgAppSecretStore) -> None:
    pg_store.save(AppSecret(app_id=APP_A, secret_key=SECRET, scope="basic"))

    loaded = pg_store.get(APP_A)
    assert loaded is not None and loaded.secret_key == SECRET
    assert [app.app_id for app in pg_store.list()] == [APP_A]


@pg_only
def test_pg_secret_is_encrypted(pg_store: PgAppSecretStore, pg_schema: str) -> None:
    pg_store.save(AppSecret(app_id=APP_A, secret_key=SECRET))
    import psycopg

    with psycopg.connect(DSN, options=f"-c search_path={pg_schema}") as conn:
        row = conn.execute("SELECT cipher FROM app_secrets").fetchone()

    assert SECRET.encode() not in bytes(row[0])


@pg_only
def test_pg_upsert_replaces_secret(pg_store: PgAppSecretStore) -> None:
    pg_store.save(AppSecret(app_id=APP_A, secret_key=SECRET))
    pg_store.save(AppSecret(app_id=APP_A, secret_key="new-secret-value"))

    loaded = pg_store.get(APP_A)
    assert loaded is not None and loaded.secret_key == "new-secret-value"


@pg_only
def test_pg_delete(pg_store: PgAppSecretStore) -> None:
    pg_store.save(AppSecret(app_id=APP_A, secret_key=SECRET))
    pg_store.delete(APP_A)

    assert pg_store.get(APP_A) is None
