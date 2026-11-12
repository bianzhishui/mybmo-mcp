"""租户映射（ADR 0008）：client_id 解析、自动开户、维护操作。"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from mybmo_mcp.auth.tenant_map import PgTenantMap, default_tenant_name, new_tenant_id
from mybmo_mcp.baidu.errors import CredentialError

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = [
    (ROOT / "migrations" / "001_init.sql").read_text(encoding="utf-8"),
    (ROOT / "migrations" / "005_tenant_client.sql").read_text(encoding="utf-8"),
]
DSN = os.environ.get("MYBMO_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not DSN, reason="需要 MYBMO_TEST_DATABASE_URL 才能跑 PG 集成测试")

CLIENT_A = "709a8e16656a223a1ab7"
CLIENT_B = "60fda1d9bedd4615eb90"


@pytest.fixture
def tmap():
    import psycopg
    from psycopg.rows import dict_row

    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET search_path TO {schema}")
        for sql in MIGRATIONS:
            conn.execute(sql)

    def connect():
        return psycopg.connect(DSN, options=f"-c search_path={schema}", row_factory=dict_row)

    yield PgTenantMap(DSN, connect=connect)

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


def test_new_tenant_id_and_name_shape() -> None:
    tenant_id = new_tenant_id()

    assert tenant_id.startswith("t_")
    assert len(tenant_id) == 2 + 12
    assert default_tenant_name(tenant_id) == f"租户-{tenant_id[-4:]}"


def test_unknown_client_is_auto_provisioned(tmap: PgTenantMap) -> None:
    tenant_id = tmap.resolve(CLIENT_A)

    row = tmap.get(tenant_id)
    assert row is not None
    assert row.client_id == CLIENT_A
    assert row.name.startswith("租户-")
    assert row.users == 0 and row.accounts == 0


def test_resolve_is_idempotent(tmap: PgTenantMap) -> None:
    first = tmap.resolve(CLIENT_A)
    second = tmap.resolve(CLIENT_A)

    assert first == second
    assert len(tmap.list()) == 1


def test_two_clients_get_two_tenants(tmap: PgTenantMap) -> None:
    assert tmap.resolve(CLIENT_A) != tmap.resolve(CLIENT_B)
    assert len(tmap.list()) == 2


def test_set_client_binds_existing_tenant(tmap: PgTenantMap) -> None:
    tenant_id = tmap.resolve(CLIENT_A)
    tmap.set_client(tenant_id, CLIENT_B)

    assert tmap.resolve(CLIENT_B) == tenant_id
    assert tmap.get(tenant_id).client_id == CLIENT_B


def test_set_client_rejects_client_of_another_tenant(tmap: PgTenantMap) -> None:
    tmap.resolve(CLIENT_A)
    b = tmap.resolve(CLIENT_B)

    with pytest.raises(CredentialError, match="只能属于一个租户"):
        tmap.set_client(b, CLIENT_A)


def test_rename(tmap: PgTenantMap) -> None:
    tenant_id = tmap.resolve(CLIENT_A)
    tmap.rename(tenant_id, "玛卡快消")

    assert tmap.get(tenant_id).name == "玛卡快消"


def test_empty_client_id_is_refused(tmap: PgTenantMap) -> None:
    tenant_id = tmap.resolve(CLIENT_A)

    with pytest.raises(CredentialError, match="不能为空"):
        tmap.set_client(tenant_id, "")


def test_unbound_lists_legacy_tenant(tmap: PgTenantMap) -> None:

    # 模拟一个没有 client_id 的历史租户
    with tmap._conn() as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES ('t_legacy', '老租户')")

    assert tmap.unbound() == ["t_legacy"]

    tmap.set_client("t_legacy", CLIENT_A)
    assert tmap.unbound() == []


def test_stats_counts_rows(tmap: PgTenantMap) -> None:
    tmap.resolve(CLIENT_A)

    stats = tmap.stats()

    assert stats["tenants"] == 1
    assert stats["users"] == 0
    assert stats["accounts"] == 0
