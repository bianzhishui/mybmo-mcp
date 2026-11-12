"""PgTenancyStore 集成测试。

需要真实 PostgreSQL：设 ``MYBMO_TEST_DATABASE_URL`` 才会跑，否则跳过。
每个用例建一个**临时 schema**，跑一遍 migrations/001_init.sql，用完删掉——
互不干扰，也不碰正式数据。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from tests.support.pg import all_migrations

from mybmo_mcp.auth.pg_tenancy import PgTenancyStore
from mybmo_mcp.auth.tenancy import AccessDenied, Principal, TenancyError

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = all_migrations()
DSN = os.environ.get("MYBMO_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not DSN, reason="需要 MYBMO_TEST_DATABASE_URL 才能跑 PG 集成测试")

ALICE = "alice-sub"
BOB = "bob-sub"


@pytest.fixture
def store():
    import psycopg

    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET search_path TO {schema}")
        for sql in MIGRATIONS:
            conn.execute(sql)

    def connect():
        # 与生产路径一致：必须用 dict_row，仓储层按列名取值
        from psycopg.rows import dict_row

        return psycopg.connect(DSN, options=f"-c search_path={schema}", row_factory=dict_row)

    yield PgTenancyStore(DSN, tenant_id="t_default", connect=connect)

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


def test_owner_has_admin(store: PgTenancyStore) -> None:
    store.register_account("main", owner=ALICE, user_name="账户")

    assert store.owner_of("main") == ALICE
    assert store.permission_for(Principal(sub=ALICE), "main") == "admin"
    assert store.stats() == {"tenants": 1, "users": 1, "accounts": 1, "grants": 0}


def test_share_and_revoke(store: PgTenancyStore) -> None:
    store.register_account("main", owner=ALICE)

    store.share("main", sub=BOB, permission="read")
    assert store.permission_for(Principal(sub=BOB), "main") == "read"
    assert store.shares_of("main") == {BOB: "read"}

    store.share("main", sub=BOB, permission="write")
    assert store.permission_for(Principal(sub=BOB), "main") == "write"

    assert store.revoke("main", sub=BOB) is True
    assert store.permission_for(Principal(sub=BOB), "main") is None


def test_admin_cannot_be_shared(store: PgTenancyStore) -> None:
    store.register_account("main", owner=ALICE)

    with pytest.raises(TenancyError):
        store.share("main", sub=BOB, permission="admin")


def test_unknown_alias_and_sub_are_denied(store: PgTenancyStore) -> None:
    store.register_account("main", owner=ALICE)

    assert store.permission_for(Principal(sub=ALICE), "nope") is None
    assert store.permission_for(Principal(sub="stranger"), "main") is None
    with pytest.raises(AccessDenied):
        store.require(Principal(sub="stranger"), "main", "read")


def test_require_rejects_insufficient_level(store: PgTenancyStore) -> None:
    store.register_account("main", owner=ALICE)
    store.share("main", sub=BOB, permission="read")

    store.require(Principal(sub=BOB), "main", "read")
    with pytest.raises(AccessDenied):
        store.require(Principal(sub=BOB), "main", "write")


def test_tenant_isolation(store: PgTenancyStore) -> None:
    store.register_account("main", owner=ALICE)
    other = PgTenancyStore(DSN, tenant_id="t_other", connect=store._connect_override)

    assert other.accounts() == []
    assert other.owner_of("main") is None
    assert other.permission_for(Principal(sub=ALICE), "main") is None


def test_describe_shape(store: PgTenancyStore) -> None:
    store.register_account("main", owner=ALICE, user_name="账户")
    store.share("main", sub=BOB, permission="read")

    owner_view = store.describe(Principal(sub=ALICE))
    bob_view = store.describe(Principal(sub=BOB))

    assert owner_view["owned"] == {"main": "admin"}
    assert owner_view["grants"] == {}
    assert bob_view["owned"] == {}
    assert bob_view["grants"] == {"main": "read"}


def test_resolve_principal_backfills_empty_display_name(store) -> None:
    """用户行是绑定时建的，那时拿不到名字；第一次带 name 的请求要补上并落库。"""
    store.register_account("main", owner=ALICE)

    first = store.resolve_principal(ALICE, fallback_name="张三")
    assert first.display_name == "张三"

    # 再问一次、换一个 fallback：说明已经落库，不是每次都用令牌里的名字
    second = store.resolve_principal(ALICE, fallback_name="别的人")
    assert second.display_name == "张三"


def test_resolve_principal_without_name_keeps_it_empty(store) -> None:
    store.register_account("main", owner=ALICE)

    assert store.resolve_principal(ALICE).display_name == ""


def test_visible_filters_by_permission(store: PgTenancyStore) -> None:
    """没有隐式全权身份之后，visible 必须逐条判权（曾漏改导致线上 500）。"""
    store.register_account("main", owner=ALICE)
    store.register_account("other", owner=BOB)

    assert store.visible(Principal(sub=ALICE), ["main", "other"]) == ["main"]
    assert store.visible(Principal(sub=BOB), ["main", "other"]) == ["other"]
    assert store.visible(Principal(sub="stranger-sub"), ["main", "other"]) == []
