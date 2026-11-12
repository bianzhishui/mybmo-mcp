"""存储契约：同一组断言同时跑 PostgreSQL 与测试替身。

为什么需要它：2026-11-12 线上出过一次 `AttributeError: 'Principal' object has no
attribute 'unrestricted'`——内存替身改对了、PG 实现漏改，单测全绿但线上 500。
两份实现就该由同一组断言来钉，而不是各测各的。

PG 需要 ``MYBMO_TEST_DATABASE_URL``，没有就跳过（内存那半永远跑）。
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from tests.support.memory import (
    MemoryAppSecretStore,
    MemoryCredentialStore,
    MemoryTenancyStore,
)
from tests.support.pg import all_migrations, seed_tenant

from mybmo_mcp.auth.app_secrets import AppSecret, PgAppSecretStore
from mybmo_mcp.auth.models import Credential
from mybmo_mcp.auth.pg_store import PgCredentialStore
from mybmo_mcp.auth.pg_tenancy import PgTenancyStore
from mybmo_mcp.auth.tenancy import (
    PERMISSION_ADMIN,
    PERMISSION_READ,
    PERMISSION_WRITE,
    AccessDenied,
    Principal,
    TenancyError,
)
from mybmo_mcp.baidu.errors import CredentialError

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("MYBMO_TEST_DATABASE_URL", "")
KEY = b"k" * 32
TENANT = "t_default"
ALICE = "alice-sub"
BOB = "bob-sub"


class _Bundle:
    def __init__(self, tenancy: Any, store: Any, app_secrets: Any, cleanup: Any = None) -> None:
        self.tenancy = tenancy
        self.store = store
        self.app_secrets = app_secrets
        self._cleanup = cleanup

    def close(self) -> None:
        if self._cleanup is not None:
            self._cleanup()


@pytest.fixture(params=["memory", "pg"])
def bundle(request):
    if request.param == "memory":
        yield _Bundle(MemoryTenancyStore(), MemoryCredentialStore(), MemoryAppSecretStore())
        return

    if not DSN:
        pytest.skip("需要 MYBMO_TEST_DATABASE_URL 才能跑 PG 那半")

    import psycopg
    from psycopg.rows import dict_row

    schema = f"t_{uuid.uuid4().hex[:12]}"
    migrations = all_migrations()
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET search_path TO {schema}")
        for sql in migrations:
            conn.execute(sql)
        seed_tenant(conn)

    def connect():
        return psycopg.connect(DSN, options=f"-c search_path={schema}", row_factory=dict_row)

    def cleanup():
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")

    try:
        yield _Bundle(
            PgTenancyStore(DSN, tenant_id=TENANT, connect=connect),
            PgCredentialStore(DSN, KEY, tenant_id=TENANT, connect=connect),
            PgAppSecretStore(DSN, KEY, tenant_id=TENANT, connect=connect),
            cleanup,
        )
    finally:
        cleanup()


def _credential(alias: str = "axd", **overrides: Any) -> Credential:
    fields: dict[str, Any] = {
        "alias": alias,
        "user_name": "示例账户",
        "user_id": 1001,
        "app_id": "20426c0457c842036585e268d456fe45",
        "secret_key": "sk-1",
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "expires_at": time.time() + 3600,
        "scope": {"tool": True},
    }
    fields.update(overrides)
    return Credential(**fields)


# ---------------------------------------------------------------- 授权表契约


def test_owner_has_admin_without_grant(bundle: _Bundle) -> None:
    bundle.tenancy.register_account("main", owner=ALICE)

    assert bundle.tenancy.permission_for(Principal(sub=ALICE), "main") == PERMISSION_ADMIN
    assert bundle.tenancy.owner_of("main") == ALICE


def test_stranger_sees_nothing(bundle: _Bundle) -> None:
    """别人的账户在 list_accounts 里根本不可见——不是"看到但拒绝"。"""
    bundle.tenancy.register_account("main", owner=ALICE)
    bob = Principal(sub=BOB)

    assert bundle.tenancy.permission_for(bob, "main") is None
    assert bundle.tenancy.visible(bob, ["main"]) == []
    with pytest.raises(AccessDenied):
        bundle.tenancy.require(bob, "main", PERMISSION_READ)


def test_share_grants_read_then_write(bundle: _Bundle) -> None:
    bundle.tenancy.register_account("main", owner=ALICE)
    bundle.tenancy.share("main", sub=BOB, permission=PERMISSION_READ)
    bob = Principal(sub=BOB)

    assert bundle.tenancy.permission_for(bob, "main") == PERMISSION_READ
    assert bundle.tenancy.visible(bob, ["main"]) == ["main"]
    bundle.tenancy.require(bob, "main", PERMISSION_READ)
    with pytest.raises(AccessDenied):
        bundle.tenancy.require(bob, "main", PERMISSION_WRITE)

    bundle.tenancy.share("main", sub=BOB, permission=PERMISSION_WRITE)
    bundle.tenancy.require(bob, "main", PERMISSION_WRITE)


def test_admin_cannot_be_granted(bundle: _Bundle) -> None:
    bundle.tenancy.register_account("main", owner=ALICE)

    with pytest.raises(TenancyError):
        bundle.tenancy.share("main", sub=BOB, permission=PERMISSION_ADMIN)


def test_revoke_removes_grant(bundle: _Bundle) -> None:
    bundle.tenancy.register_account("main", owner=ALICE)
    bundle.tenancy.share("main", sub=BOB, permission=PERMISSION_READ)

    assert bundle.tenancy.revoke("main", sub=BOB) is True
    assert bundle.tenancy.permission_for(Principal(sub=BOB), "main") is None
    assert bundle.tenancy.revoke("main", sub=BOB) is False


def test_unregister_removes_owner_and_shares(bundle: _Bundle) -> None:
    bundle.tenancy.register_account("main", owner=ALICE)
    bundle.tenancy.share("main", sub=BOB, permission=PERMISSION_READ)

    assert bundle.tenancy.unregister_account("main") is True
    assert bundle.tenancy.owner_of("main") is None
    assert bundle.tenancy.shares_of("main") == {}
    assert bundle.tenancy.unregister_account("main") is False


def test_describe_separates_owned_and_shared(bundle: _Bundle) -> None:
    bundle.tenancy.register_account("main", owner=ALICE)
    bundle.tenancy.register_account("second", owner=BOB)
    bundle.tenancy.share("second", sub=ALICE, permission=PERMISSION_READ)

    alice = bundle.tenancy.describe(Principal(sub=ALICE))

    assert alice["owned"] == {"main": PERMISSION_ADMIN}
    assert alice["grants"] == {"second": PERMISSION_READ}


def test_stats_counts_rows(bundle: _Bundle) -> None:
    bundle.tenancy.register_account("main", owner=ALICE)
    bundle.tenancy.share("main", sub=BOB, permission=PERMISSION_READ)

    stats = bundle.tenancy.stats()

    assert stats["accounts"] == 1
    assert stats["grants"] == 1


# ---------------------------------------------------------------- 凭据契约


def test_credential_roundtrip_and_delete(bundle: _Bundle) -> None:
    bundle.store.save(_credential())

    assert bundle.store.load("axd").access_token == "at-1"
    assert bundle.store.aliases() == ["axd"]

    bundle.store.delete("axd")
    bundle.store.delete("axd")  # 幂等
    assert bundle.store.aliases() == []


def test_credential_default_alias(bundle: _Bundle) -> None:
    bundle.store.save(_credential("a"))
    bundle.store.save(_credential("b", user_id=2002, app_id="d83aa97522af9aa92a59892993085c80"))

    bundle.store.set_default("a")
    assert bundle.store.default_alias() == "a"

    bundle.store.set_default("b")
    assert bundle.store.default_alias() == "b"


def test_credential_locked_handle_writes_back(bundle: _Bundle) -> None:
    """刷新写回必须走锁句柄——这是并发安全的核心契约。"""
    bundle.store.save(_credential("a"))

    with bundle.store.locked("a") as row:
        current = row.credential
        assert current.access_token == "at-1"
        row.save(current.with_tokens(access_token="at-new"))

    assert bundle.store.load("a").access_token == "at-new"


def test_credential_locked_rejects_unknown(bundle: _Bundle) -> None:
    with pytest.raises(CredentialError):
        with bundle.store.locked("nope"):
            pass


# ---------------------------------------------------------------- 应用密钥契约


def test_app_secret_roundtrip(bundle: _Bundle) -> None:
    app_id = "20426c0457c842036585e268d456fe45"
    bundle.app_secrets.save(AppSecret(app_id=app_id, secret_key="sk-1", scope="basic"))

    loaded = bundle.app_secrets.get(app_id)
    assert loaded is not None
    assert loaded.secret_key == "sk-1"
    assert loaded.scope == "basic"
    assert [a.app_id for a in bundle.app_secrets.list()] == [app_id]

    bundle.app_secrets.delete(app_id)
    assert bundle.app_secrets.get(app_id) is None


def test_app_secret_never_leaks_in_repr(bundle: _Bundle) -> None:
    secret = AppSecret(app_id="20426c0457c842036585e268d456fe45", secret_key="sk-1")

    assert "sk-1" not in repr(secret)
    assert "secret_key" not in secret.to_public()
