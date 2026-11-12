"""迁移 008 必须**保住数据**。

空 schema 上跑不出问题——PL/pgSQL 的语句要到第一次执行才解析，表里没数据时
那个 INSERT 根本不会跑（第一次部署时就踩了这个坑：AmbiguousColumn 只在有数据时暴露）。

所以这里先按 001~007 建老结构、灌一批数据，再跑 008，逐项核对：
主键重映射、外键关系、业务时间列合并、日志 id 保留。
"""

from __future__ import annotations

import os
import uuid

import pytest
from tests.support.pg import MIGRATIONS_DIR, all_migrations

DSN = os.environ.get("MYBMO_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not DSN, reason="需要 MYBMO_TEST_DATABASE_URL 才能跑 PG 集成测试")

BEFORE = ["001_init.sql", "002_audit_log.sql", "003_credentials.sql",
          "004_drop_users_role.sql", "005_tenant_client.sql",
          "006_binding_states.sql", "007_subscribe_events.sql"]


@pytest.fixture
def schema():
    import psycopg
    from psycopg.rows import dict_row

    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {name}")

    def connect():
        return psycopg.connect(DSN, options=f"-c search_path={name}", row_factory=dict_row)

    yield name, connect

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {name} CASCADE")


def _run(conn, filenames: list[str]) -> None:
    """按文件名跑迁移。"""
    for filename in filenames:
        conn.execute((MIGRATIONS_DIR / filename).read_text(encoding="utf-8"))


def _run_sql(conn, statements: list[str]) -> None:
    """直接跑 SQL 文本列表（all_migrations() 的返回值）。"""
    for sql in statements:
        conn.execute(sql)


def test_migration_keeps_data(schema) -> None:
    _, connect = schema
    user_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())

    with connect() as conn:
        _run(conn, BEFORE)
        conn.execute(
            "INSERT INTO tenants (id, name, client_id) VALUES ('t_default','t_default','cid-1')"
        )
        conn.execute(
            "INSERT INTO users (id, tenant_id, idp_subject, display_name)"
            " VALUES (%s,'t_default','sub-1','张三')",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO baidu_accounts (id, tenant_id, alias, owner_user_id, user_name)"
            " VALUES (%s,'t_default','main',%s,'账户')",
            (account_id, user_id),
        )
        conn.execute(
            "INSERT INTO account_grants (user_id, account_id, permission)"
            " VALUES (%s,%s,'read')",
            (user_id, account_id),
        )
        conn.execute(
            "INSERT INTO credentials (tenant_id, alias, app_id, user_name, nonce, cipher)"
            " VALUES ('t_default','main','app-1','账户','\\x00','\\x01')"
        )
        conn.execute(
            "INSERT INTO app_secrets (tenant_id, app_id, nonce, cipher)"
            " VALUES ('t_default','app-1','\\x00','\\x01')"
        )
        conn.execute(
            "INSERT INTO binding_states (nonce, tenant_id, subject, alias, issued_at, expires_at)"
            " VALUES ('n-1','t_default','sub-1','main',"
            "         to_timestamp(1700000000), to_timestamp(1700000900))"
        )
        conn.execute(
            "INSERT INTO audit_log (id, call_id, started_at, duration_ms, tenant_id,"
            " user_sub, capability, state)"
            " OVERRIDING SYSTEM VALUE"
            " VALUES (42,'call-1',to_timestamp(1700000000),5,'t_default','sub-1','X.y','succeeded')"
        )
        conn.execute(
            "INSERT INTO subscribe_events (id, method, path, received_at)"
            " OVERRIDING SYSTEM VALUE"
            " VALUES (7,'POST','/oauth/baidu/subscribe',to_timestamp(1700000000))"
        )
        conn.commit()

    with connect() as conn:
        _run(conn, ["008_table_convention.sql"])
        conn.commit()

        tenant = conn.execute("SELECT id, tenant_key, client_id FROM tenants").fetchone()
        assert tenant["tenant_key"] == "t_default"
        assert tenant["client_id"] == "cid-1"
        assert isinstance(tenant["id"], int)

        user = conn.execute(
            "SELECT id, display_name FROM users WHERE idp_subject = 'sub-1'"
        ).fetchone()
        assert isinstance(user["id"], int)
        assert user["display_name"] == "张三"

        account = conn.execute(
            "SELECT id, owner_user_id, alias FROM baidu_accounts WHERE alias = 'main'"
        ).fetchone()
        assert isinstance(account["id"], int)
        assert account["owner_user_id"] == user["id"]  # 重映射后仍指向同一个用户

        grant = conn.execute(
            "SELECT user_id, account_id, permission FROM account_grants"
        ).fetchone()
        assert grant["user_id"] == user["id"]
        assert grant["account_id"] == account["id"]
        assert grant["permission"] == "read"

        assert conn.execute("SELECT alias FROM credentials").fetchone()["alias"] == "main"
        assert conn.execute("SELECT app_id FROM app_secrets").fetchone()["app_id"] == "app-1"

        state = conn.execute(
            "SELECT extract(epoch FROM created_at) AS created,"
            " extract(epoch FROM expires_at) AS expires FROM binding_states"
        ).fetchone()
        assert int(state["created"]) == 1700000000  # issued_at 并入 created_at
        assert int(state["expires"]) == 1700000900

        audit = conn.execute("SELECT id, call_id FROM audit_log").fetchone()
        assert audit["id"] == 42  # 旧 id 保留（OVERRIDING SYSTEM VALUE）
        assert audit["call_id"] == "call-1"

        sub = conn.execute(
            "SELECT id, extract(epoch FROM created_at) AS created FROM subscribe_events"
        ).fetchone()
        assert sub["id"] == 7
        assert int(sub["created"]) == 1700000000  # received_at 并入 created_at


def test_new_rows_get_identity_ids(schema) -> None:
    """重建之后自增序列可用（含 audit_log / subscribe_events 的 setval）。"""
    _, connect = schema
    with connect() as conn:
        _run_sql(conn, all_migrations())
        conn.commit()

        conn.execute("INSERT INTO tenants (tenant_key, name) VALUES ('t_x','x')")
        conn.execute("INSERT INTO tenants (tenant_key, name) VALUES ('t_y','y')")
        ids = [r["id"] for r in conn.execute("SELECT id FROM tenants ORDER BY id").fetchall()]
        assert ids == sorted(ids) and len(set(ids)) == 2

        conn.execute(
            "INSERT INTO audit_log (call_id, started_at, tenant_id, user_sub, capability, state)"
            " VALUES ('c-1', now(), 't_x', 's', 'A.b', 'succeeded')"
        )
        row = conn.execute("SELECT id FROM audit_log").fetchone()
        assert row["id"] == 1
        conn.commit()
