"""迁移机制：文件发现与 dry-run 不需要数据库。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_runner():
    target = ROOT / "migrations" / "run.py"
    spec = importlib.util.spec_from_file_location("migration_runner", target)
    module = importlib.util.module_from_spec(spec)
    sys.modules["migration_runner"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_discover_finds_initial_migration() -> None:
    runner = load_runner()

    found = runner.discover()

    assert [version for version, _ in found] == [1]
    assert found[0][1].name == "001_init.sql"


def test_dry_run_lists_pending_versions() -> None:
    runner = load_runner()

    assert runner.apply("postgresql://unused", dry_run=True) == [1]


def test_migration_sql_declares_core_tables() -> None:
    sql = (ROOT / "migrations" / "001_init.sql").read_text(encoding="utf-8")

    for table in ("schema_version", "tenants", "users", "baidu_accounts", "account_grants"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    # 账户私有：归属列必须在
    assert "owner_user_id" in sql
    # admin 不可转授：grant 只允许 read/write
    assert "CHECK (permission IN ('read', 'write'))" in sql
