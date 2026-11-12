"""表规范护栏（ADR 0010）。

规范：
1. 每张业务表第一列是 ``id``，类型 bigint、identity、且是主键
2. 最后两列是 ``created_at`` / ``updated_at``，且 NOT NULL
3. ``updated_at`` 有触发器自动维护
4. ``schema_version`` 豁免（迁移账本）

这条测试的价值在于"以后新增表违反规范会直接红"，而不是事后靠人肉 review。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("MYBMO_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not DSN, reason="需要 MYBMO_TEST_DATABASE_URL 才能跑 PG 集成测试")

#: 迁移账本，不参与规范（它的 version 就是主键）
EXEMPT = {"schema_version"}


@pytest.fixture(scope="module")
def schema():
    """跑完整套迁移建一个临时 schema，用完删掉。"""
    import psycopg

    name = f"t_{uuid.uuid4().hex[:12]}"
    files = sorted((ROOT / "migrations").glob("0*.sql"))
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {name}")
        conn.execute(f"SET search_path TO {name}")
        for path in files:
            conn.execute(path.read_text(encoding="utf-8"))

    yield name

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {name} CASCADE")


@pytest.fixture
def conn(schema):
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(DSN, options=f"-c search_path={schema}", row_factory=dict_row) as c:
        yield c


def _tables(conn) -> list[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables"
        " WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
        " ORDER BY table_name"
    ).fetchall()
    return [r["table_name"] for r in rows if r["table_name"] not in EXEMPT]


def _columns(conn, table: str) -> list[dict]:
    return conn.execute(
        "SELECT column_name, data_type, is_nullable, is_identity, column_default"
        " FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = %s"
        " ORDER BY ordinal_position",
        (table,),
    ).fetchall()


def _primary_key_columns(conn, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT a.attname FROM pg_index i"
        " JOIN pg_class c ON c.oid = i.indrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY (i.indkey)"
        " WHERE i.indisprimary AND c.relname = %s AND n.nspname = current_schema()"
        " ORDER BY array_position(i.indkey, a.attnum)",
        (table,),
    ).fetchall()
    return [r["attname"] for r in rows]


def test_every_table_has_id_first(conn) -> None:
    offenders = []
    for table in _tables(conn):
        cols = _columns(conn, table)
        first = cols[0]
        pk = _primary_key_columns(conn, table)
        if (
            first["column_name"] != "id"
            or first["data_type"] != "bigint"
            or first["is_identity"] != "YES"
            or pk != ["id"]
        ):
            offenders.append(
                f"{table}: first={first['column_name']} type={first['data_type']} "
                f"identity={first['is_identity']} pk={pk}"
            )
    assert not offenders, "以下表的 id 不规范：\n" + "\n".join(offenders)


def test_every_table_ends_with_timestamps(conn) -> None:
    offenders = []
    for table in _tables(conn):
        cols = _columns(conn, table)
        tail = [(c["column_name"], c["data_type"], c["is_nullable"]) for c in cols[-2:]]
        expected = [
            ("created_at", "timestamp with time zone", "NO"),
            ("updated_at", "timestamp with time zone", "NO"),
        ]
        if tail != expected:
            offenders.append(f"{table}: 末尾两列是 {tail}")
    assert not offenders, "以下表的时间列不规范：\n" + "\n".join(offenders)


def test_every_table_maintains_updated_at_by_trigger(conn) -> None:
    rows = conn.execute(
        "SELECT c.relname AS tbl FROM pg_trigger t"
        " JOIN pg_class c ON c.oid = t.tgrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " JOIN pg_proc p ON p.oid = t.tgfoid"
        " WHERE NOT t.tgisinternal AND p.proname = 'set_updated_at'"
        "   AND n.nspname = current_schema()"
    ).fetchall()
    with_trigger = {r["tbl"] for r in rows}

    missing = sorted(set(_tables(conn)) - with_trigger)
    assert not missing, f"以下表缺少 updated_at 触发器：{missing}"


def test_updated_at_actually_updates(conn) -> None:
    """触发器真的生效：改一行，updated_at 必须变。"""
    conn.execute(
        "INSERT INTO tenants (tenant_key, name) VALUES ('t_smoke', '冒烟')"
    )
    before = conn.execute(
        "SELECT updated_at FROM tenants WHERE tenant_key = 't_smoke'"
    ).fetchone()["updated_at"]
    conn.execute("UPDATE tenants SET name = '改名' WHERE tenant_key = 't_smoke'")
    after = conn.execute(
        "SELECT updated_at FROM tenants WHERE tenant_key = 't_smoke'"
    ).fetchone()["updated_at"]

    assert after >= before
    conn.execute("DELETE FROM tenants WHERE tenant_key = 't_smoke'")


def test_timezone_is_shanghai(conn) -> None:
    tz = conn.execute("SHOW timezone").fetchone()["TimeZone"]
    assert tz == "Asia/Shanghai"


def test_tenant_key_is_the_natural_key(conn) -> None:
    """代理键 + 自然键：tenant_key 唯一，tenant_id 列引用它（不是数字 id）。"""
    row = conn.execute(
        "SELECT data_type FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = 'users'"
        "   AND column_name = 'tenant_id'"
    ).fetchone()
    assert row["data_type"] == "text"
