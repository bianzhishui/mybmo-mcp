"""绑定 state 落库（ADR 0009 D6）：一次性、TTL、过期清理。"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from mybmo_mcp.auth.binding import PgBindingStateStore

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = [
    (ROOT / "migrations" / "006_binding_states.sql").read_text(encoding="utf-8"),
]
DSN = os.environ.get("MYBMO_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not DSN, reason="需要 MYBMO_TEST_DATABASE_URL 才能跑 PG 集成测试")


@pytest.fixture
def store_factory():
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

    def make(**kwargs):
        return PgBindingStateStore(DSN, connect=connect, **kwargs)

    yield make

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


def test_issue_then_consume_roundtrip(store_factory) -> None:
    store = store_factory()

    state = store.issue(subject="sub-1", tenant="t_default", alias="main", app_id="app-x")
    consumed = store.consume(state.nonce)

    assert consumed is not None
    assert consumed.subject == "sub-1"
    assert consumed.tenant == "t_default"
    assert consumed.alias == "main"
    assert consumed.app_id == "app-x"


def test_consume_is_one_time(store_factory) -> None:
    """并发/重放都只有一个请求能拿到——靠 DELETE … RETURNING。"""
    store = store_factory()
    state = store.issue(subject="s", tenant="t", alias="main")

    assert store.consume(state.nonce) is not None
    assert store.consume(state.nonce) is None


def test_expired_state_is_rejected(store_factory) -> None:
    clock = [1000.0]
    store = store_factory(ttl=10, now=lambda: clock[0])
    state = store.issue(subject="s", tenant="t", alias="main")

    clock[0] = 1020.0

    assert store.consume(state.nonce) is None


def test_suspicious_nonce_is_rejected(store_factory) -> None:
    store = store_factory()

    for nonce in ("", "../../etc/passwd", "short", "a" * 200):
        assert store.consume(nonce) is None


def test_issue_purges_expired_rows(store_factory) -> None:
    """签发时顺手清过期行：省掉一个定时任务，也避免表无限增长。"""
    clock = [1000.0]
    store = store_factory(ttl=10, now=lambda: clock[0])
    store.issue(subject="s", tenant="t", alias="main")

    clock[0] = 2000.0
    store.issue(subject="s2", tenant="t", alias="main")

    conn = store._connect_override()  # noqa: SLF001 - 测试里直接查行数
    try:
        rows = conn.execute("SELECT nonce FROM binding_states").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
