"""按文件名顺序执行未应用的迁移。

用法::

    python migrations/run.py            # 用 MYBMO_DATABASE_URL
    python migrations/run.py --dry-run  # 只打印将要执行的文件

约定（见 openspec/changes/multi-tenant-persistence）：

* 文件名 ``NNN_描述.sql``，数字即版本号，必须严格递增且不重复
* 每个文件在**单个事务**里执行，并写入 ``schema_version``；失败整体回滚，不留半套表
* 重复执行幂等：已记录的版本直接跳过
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent


def discover() -> list[tuple[int, Path]]:
    """收集迁移文件，按版本号排序。版本号重复或非法直接报错。"""
    found: dict[int, Path] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        stem = path.stem
        head, _, _ = stem.partition("_")
        if not head.isdigit():
            raise SystemExit(f"迁移文件名必须以版本号开头：{path.name}")
        version = int(head)
        if version in found:
            raise SystemExit(f"版本号重复：{version}（{found[version].name} 与 {path.name}）")
        found[version] = path
    return sorted(found.items())


def apply(dsn: str, *, dry_run: bool = False, schema: str | None = None) -> list[int]:
    """执行迁移，返回本次实际应用的版本号。

    ``schema`` 只给测试用：把整套表建在临时 schema 里，跑完即删，不碰正式数据。
    """
    import psycopg

    pending_all = discover()
    if dry_run:
        return [version for version, _ in pending_all]

    applied: list[int] = []
    options = f"-c search_path={schema}" if schema else None
    with psycopg.connect(dsn, autocommit=True, options=options) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            cur.execute("SELECT version FROM schema_version")
            done = {row[0] for row in cur.fetchall()}

        for version, path in pending_all:
            if version in done:
                continue
            sql = path.read_text(encoding="utf-8")
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute("INSERT INTO schema_version (version) VALUES (%s)", (version,))
            applied.append(version)
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行数据库迁移")
    parser.add_argument("--dry-run", action="store_true", help="只列出待执行的版本")
    args = parser.parse_args(argv)

    dsn = os.environ.get("MYBMO_DATABASE_URL", "").strip()
    if not dsn:
        print("缺少 MYBMO_DATABASE_URL", file=sys.stderr)
        return 2

    applied = apply(dsn, dry_run=args.dry_run)
    if args.dry_run:
        print("待执行版本:", applied or "（无）")
    else:
        print("已应用版本:", applied or "（无，已是最新）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
