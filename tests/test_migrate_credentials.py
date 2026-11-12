"""凭据迁移脚本：字段比对与整批搬运（ADR 0007 D8）。"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

from mybmo_mcp.auth.app_secrets import AppSecret, FileAppSecretStore
from mybmo_mcp.auth.models import Credential
from mybmo_mcp.auth.pg_store import PgCredentialStore
from mybmo_mcp.auth.vault import FileVault

ROOT = Path(__file__).resolve().parents[1]
KEY = b"k" * 32
APP = "20426c0457c842036585e268d456fe45"
DSN = os.environ.get("MYBMO_TEST_DATABASE_URL", "")


def load_script():
    target = ROOT / "scripts" / "migrate_credentials_to_pg.py"
    spec = importlib.util.spec_from_file_location("migrate_credentials", target)
    module = importlib.util.module_from_spec(spec)
    sys.modules["migrate_credentials"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def credential(alias: str = "axd", **overrides: object) -> Credential:
    fields: dict[str, object] = {
        "alias": alias,
        "user_name": "示例账户",
        "user_id": 1001,
        "app_id": APP,
        "secret_key": "sk-1",
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "expires_at": time.time() + 3600,
        "scope": {"tool": True},
    }
    fields.update(overrides)
    return Credential(**fields)  # type: ignore[arg-type]


def test_diff_reports_no_problem_for_identical() -> None:
    diff = load_script().diff_credentials
    assert diff(credential(), credential()) == []


def test_diff_names_the_offending_fields() -> None:
    diff = load_script().diff_credentials
    problems = diff(credential(), credential(access_token="other", user_name="另一个"))

    assert "access_token" in problems
    assert "user_name" in problems


def test_diff_tolerates_microsecond_time_drift() -> None:
    diff = load_script().diff_credentials
    source = credential()
    loaded = credential(expires_at=source.expires_at - 0.4)

    assert diff(source, loaded) == []


@pytest.mark.skipif(not DSN, reason="需要 MYBMO_TEST_DATABASE_URL 才能跑 PG 集成测试")
def test_migrate_moves_credentials_and_apps(tmp_path: Path) -> None:
    import psycopg

    schema = f"t_{uuid.uuid4().hex[:12]}"
    migrations = [
        (ROOT / "migrations" / "001_init.sql").read_text(encoding="utf-8"),
        (ROOT / "migrations" / "003_credentials.sql").read_text(encoding="utf-8"),
        (ROOT / "migrations" / "005_tenant_client.sql").read_text(encoding="utf-8"),
    ]
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET search_path TO {schema}")
        for sql in migrations:
            conn.execute(sql)

    data_dir = tmp_path / "data"
    expected = credential()  # 只造一次：helper 里 time.time() 每次都不同
    FileVault(data_dir, KEY).save(expected)
    FileAppSecretStore(data_dir, KEY).save(AppSecret(app_id=APP, secret_key="sk-app"))
    dsn = f"{DSN}?options=-csearch_path%3D{schema}"

    try:
        report = load_script().migrate(
            data_dir=data_dir, master_key=KEY, dsn=dsn, tenant_id="t_default"
        )
        assert report.ok, report.failed
        assert sorted(report.moved) == sorted(["axd", f"app:{APP}"])

        target = PgCredentialStore(dsn, KEY, tenant_id="t_default")
        assert load_script().diff_credentials(expected, target.load("axd")) == []
        # 源文件必须原样保留
        assert FileVault(data_dir, KEY).load("axd") == expected
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
