"""PgAuditSink 集成测试：真实 PostgreSQL + 临时 schema。

设 ``MYBMO_TEST_DATABASE_URL`` 才会跑，否则跳过。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from mybmo_mcp.audit.pg import PgAuditSink

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (ROOT / "migrations" / "002_audit_log.sql").read_text(encoding="utf-8")
DSN = os.environ.get("MYBMO_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not DSN, reason="需要 MYBMO_TEST_DATABASE_URL")


@pytest.fixture
def sink():
    import psycopg
    from psycopg.rows import dict_row

    schema = f"a_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET search_path TO {schema}")
        conn.execute(MIGRATION)

    def connect():
        return psycopg.connect(DSN, options=f"-c search_path={schema}", row_factory=dict_row)

    pg_sink = PgAuditSink(DSN, connect=connect)
    yield pg_sink

    # 必须先断开 sink 的连接：fetch_rows 里那条 SELECT 会留下 idle-in-transaction，
    # 持有 audit_log 的 AccessShareLock，DROP SCHEMA 会一直等锁（实测卡死）
    pg_sink._reset()
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


def fetch_rows(sink: PgAuditSink) -> list[dict]:
    with sink._connection().cursor() as cur:
        cur.execute("SELECT * FROM audit_log ORDER BY id")
        return cur.fetchall()


def do_record(sink: PgAuditSink, **overrides):
    args = {
        "sub": "c70b2946-5f39-491f-8c3b-645dae15505a",
        "tenant_id": "t_default",
        "account": "main",
        "capability": "CampaignShoppingService.getCampaignShopping",
        "permission": "read",
        "mutating": "false",
        "state": "succeeded",
        "call_id": uuid.uuid4().hex[:12],
        "duration_ms": 277,
        "user_name": "test02",
        "app": "709a8e16656a223a1ab7",
        "account_name": "baidu-YF电商-玛卡快消01",
        "payload": {"campaignShoppingIds": [2079251858]},
        "response_body": {"data": [{"campaignShoppingId": 2079251858}], "expand": {}},
        "http_status": 200,
    }
    args.update(overrides)
    return sink.record(**args)


def test_record_writes_all_columns(sink: PgAuditSink) -> None:
    do_record(sink)

    rows = fetch_rows(sink)
    assert len(rows) == 1
    row = rows[0]
    assert row["tenant_id"] == "t_default"
    assert row["user_sub"] == "c70b2946-5f39-491f-8c3b-645dae15505a"
    assert row["user_name"] == "test02"
    assert row["app"] == "709a8e16656a223a1ab7"
    assert row["account_alias"] == "main"
    assert row["account_name"] == "baidu-YF电商-玛卡快消01"
    assert row["capability"] == "CampaignShoppingService.getCampaignShopping"
    assert row["state"] == "succeeded"
    assert row["http_status"] == 200
    assert row["duration_ms"] == 277
    assert row["started_at"] is not None and row["finished_at"] is not None
    # 入参/出参完整写入，字段顺序无关
    assert row["request_params"] == {"campaignShoppingIds": [2079251858]}
    assert row["response_body"] == {
        "data": [{"campaignShoppingId": 2079251858}],
        "expand": {},
    }
    assert row["request_bytes"] > 0 and row["response_bytes"] > 0
    assert row["payload_digest"]


def test_four_states_are_accepted(sink: PgAuditSink) -> None:
    for state in ("succeeded", "failed", "unknown", "denied"):
        do_record(sink, state=state, call_id=uuid.uuid4().hex[:12])

    assert [r["state"] for r in fetch_rows(sink)] == [
        "succeeded",
        "failed",
        "unknown",
        "denied",
    ]


def test_bad_state_is_rejected_by_constraint(sink: PgAuditSink) -> None:
    # 约束挡住时 record 不能抛异常（审计失败不影响调用）
    do_record(sink, state="bogus")
    assert fetch_rows(sink) == []


def test_duplicate_call_id_does_not_raise(sink: PgAuditSink) -> None:
    call_id = uuid.uuid4().hex[:12]
    do_record(sink, call_id=call_id)
    do_record(sink, call_id=call_id)  # 唯一索引冲突 → 只记日志

    assert len(fetch_rows(sink)) == 1


def test_payload_omitted_when_store_payload_disabled() -> None:
    import psycopg

    schema = f"a_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET search_path TO {schema}")
        conn.execute(MIGRATION)

    def connect():
        return psycopg.connect(DSN, options=f"-c search_path={schema}")

    def connect_dict():
        from psycopg.rows import dict_row

        return psycopg.connect(DSN, options=f"-c search_path={schema}", row_factory=dict_row)

    sink = PgAuditSink(DSN, connect=connect_dict, store_payload=False)
    try:
        record = do_record(sink)
        assert record.request_params is None
        assert record.payload_digest  # 摘要仍在
        rows = fetch_rows(sink)
        assert rows[0]["request_params"] is None
        assert rows[0]["response_body"] is None
    finally:
        sink._reset()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def test_unreachable_database_does_not_raise() -> None:
    """PG 挂掉时审计必须静默失败——不能让一次只读查询因此报错。"""

    def broken():
        raise RuntimeError("connection refused")

    sink = PgAuditSink("postgresql://nowhere/none", connect=broken)

    record = do_record(sink)

    assert record.state == "succeeded"  # 记录本身构造成功，只是没写进去


def test_query_restricts_to_visible_subs(sink: PgAuditSink) -> None:
    do_record(sink, sub="alice", call_id=uuid.uuid4().hex[:12])
    do_record(sink, sub="bob", call_id=uuid.uuid4().hex[:12])

    total, rows = sink.query(tenant_id="t_default", visible_subs=["alice"])

    assert total == 1
    assert rows[0]["user_sub"] == "alice"


def test_query_by_alias_includes_other_users_rows(sink: PgAuditSink) -> None:
    """owner 看自己账户时，要能看到别人在该账户下的调用。"""
    do_record(sink, sub="alice", account="main", call_id=uuid.uuid4().hex[:12])
    do_record(sink, sub="bob", account="main", call_id=uuid.uuid4().hex[:12])
    do_record(sink, sub="bob", account="other", call_id=uuid.uuid4().hex[:12])

    total, rows = sink.query(
        tenant_id="t_default", visible_subs=["alice"], visible_aliases=["main"]
    )

    assert total == 2
    assert {r["user_sub"] for r in rows} == {"alice", "bob"}


def test_query_filters_and_pagination(sink: PgAuditSink) -> None:
    for state in ("succeeded", "failed", "unknown", "denied"):
        do_record(sink, state=state, call_id=uuid.uuid4().hex[:12], capability="A.b")

    total, rows = sink.query(tenant_id="t_default", state="failed")
    assert total == 1 and rows[0]["state"] == "failed"

    total, rows = sink.query(tenant_id="t_default", capability="A.")
    assert total == 4

    total, rows = sink.query(tenant_id="t_default", limit=2, offset=1)
    assert total == 4 and len(rows) == 2


def test_query_params_match(sink: PgAuditSink) -> None:
    do_record(sink, call_id=uuid.uuid4().hex[:12], payload={"campaignShoppingIds": [2079251858]})
    do_record(sink, call_id=uuid.uuid4().hex[:12], payload={"campaignShoppingIds": [1]})

    total, _ = sink.query(tenant_id="t_default", params_match={"campaignShoppingIds": [2079251858]})

    assert total == 1


def test_query_hides_payload_unless_requested(sink: PgAuditSink) -> None:
    do_record(sink, call_id=uuid.uuid4().hex[:12])

    _, without = sink.query(tenant_id="t_default", include_payload=False)
    _, with_payload = sink.query(tenant_id="t_default", include_payload=True)

    assert "request_params" not in without[0]
    assert with_payload[0]["request_params"] == {"campaignShoppingIds": [2079251858]}


def test_query_is_tenant_scoped(sink: PgAuditSink) -> None:
    do_record(sink, call_id=uuid.uuid4().hex[:12], tenant_id="t_default")
    do_record(sink, call_id=uuid.uuid4().hex[:12], tenant_id="t_other")

    total, _ = sink.query(tenant_id="t_default")

    assert total == 1


def test_query_works_without_dict_row_factory() -> None:
    """生产连接没有 row_factory 时返回元组——曾因 dict(row) 直接炸掉。"""
    import psycopg

    schema = f"a_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET search_path TO {schema}")
        conn.execute(MIGRATION)

    def connect():
        return psycopg.connect(DSN, options=f"-c search_path={schema}")

    sink = PgAuditSink(DSN, connect=connect)
    try:
        do_record(sink, call_id=uuid.uuid4().hex[:12])
        total, rows = sink.query(tenant_id="t_default", include_payload=True)
        assert total == 1
        assert rows[0]["user_name"] == "test02"
        assert rows[0]["request_params"] == {"campaignShoppingIds": [2079251858]}
    finally:
        sink._reset()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
