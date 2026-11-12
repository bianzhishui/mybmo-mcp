"""订阅采样落库（ADR 0009 D6）。"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from tests.support.pg import all_migrations

from mybmo_mcp.subscribe import PgSubscribeStore, build_record

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = all_migrations()
DSN = os.environ.get("MYBMO_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not DSN, reason="需要 MYBMO_TEST_DATABASE_URL 才能跑 PG 集成测试")


@pytest.fixture
def store():
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

    yield PgSubscribeStore(DSN, connect=connect), schema

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


def _record(**overrides):
    base = dict(
        method="POST",
        path="/oauth/baidu/subscribe",
        headers={"content-type": "application/json"},
        params={"k": "v"},
        body=b'{"event":"x"}',
        secret="s3cret",
        key_source="header",
        client="1.2.3.4",
    )
    base.update(overrides)
    return build_record(**base)


def test_saves_one_row_per_push(store) -> None:
    sink, schema = store
    first = sink.save(_record())
    second = sink.save(_record())

    assert isinstance(first, int) and first > 0
    assert second > first

    import psycopg

    with psycopg.connect(DSN, options=f"-c search_path={schema}") as conn:
        rows = conn.execute("SELECT method, client, body_json FROM subscribe_events").fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "POST"
    assert rows[0][1] == "1.2.3.4"


def test_secret_is_not_stored(store) -> None:
    """脱敏发生在 build_record，落库前就该没有密钥。"""
    sink, schema = store
    sink.save(_record(body=b'{"sig":"s3cret"}'))

    import psycopg

    with psycopg.connect(DSN, options=f"-c search_path={schema}") as conn:
        row = conn.execute("SELECT body FROM subscribe_events").fetchone()
    assert "s3cret" not in row[0]
