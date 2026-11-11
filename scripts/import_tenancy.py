"""把文件式授权表（``data/tenancy.json``）一次性导入 PostgreSQL。

用法::

    python scripts/import_tenancy.py --dsn "$MYBMO_DATABASE_URL" \
        --file /data/tenancy.json --tenant t_default [--dry-run]

幂等：按 (tenant_id, idp_subject) / (tenant_id, alias) 做 upsert，可重复执行。
导入后**必须核对 owner 未丢**——丢了就等于账户无人可见。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path


def load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("version")
    if version not in (1, 2):
        raise SystemExit(f"不支持的授权表版本：{version}")
    return data


def import_data(dsn: str, data: dict, *, tenant_id: str, dry_run: bool = False) -> dict:
    users = [u for u in (data.get("users") or []) if isinstance(u, dict)]
    accounts = [a for a in (data.get("accounts") or []) if isinstance(a, dict)]
    grants = [g for g in (data.get("grants") or []) if isinstance(g, dict)]

    owners = {str(a.get("owner") or "") for a in accounts}
    missing_owner = [a.get("alias") for a in accounts if not a.get("owner")]
    if missing_owner:
        raise SystemExit(f"这些账户没有 owner，导入会丢归属：{missing_owner}")

    summary = {
        "users": len(users),
        "accounts": len(accounts),
        "grants": len(grants),
        "owners": len({o for o in owners if o}),
    }
    if dry_run:
        return summary

    import psycopg

    with psycopg.connect(dsn, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (tenant_id, tenant_id),
        )

        user_ids: dict[str, str] = {}
        for item in users:
            sub = str(item.get("sub") or "").strip()
            if not sub:
                continue
            cur.execute(
                "SELECT id FROM users WHERE tenant_id = %s AND idp_subject = %s",
                (tenant_id, sub),
            )
            row = cur.fetchone()
            if row:
                user_ids[sub] = row[0]
                cur.execute(
                    "UPDATE users SET display_name = %s WHERE id = %s",
                    (str(item.get("display_name") or ""), row[0]),
                )
            else:
                user_id = str(uuid.uuid4())
                user_ids[sub] = user_id
                cur.execute(
                    "INSERT INTO users (id, tenant_id, idp_subject, display_name)"
                    " VALUES (%s, %s, %s, %s)",
                    (user_id, tenant_id, sub, str(item.get("display_name") or "")),
                )

        for item in accounts:
            alias = str(item.get("alias") or "").strip().lower()
            owner = str(item.get("owner") or "").strip()
            if not alias or not owner:
                continue
            cur.execute(
                "SELECT id FROM users WHERE tenant_id = %s AND idp_subject = %s",
                (tenant_id, owner),
            )
            row = cur.fetchone()
            if row:
                owner_id = row[0]
            else:
                owner_id = str(uuid.uuid4())
                user_ids[owner] = owner_id
                cur.execute(
                    "INSERT INTO users (id, tenant_id, idp_subject) VALUES (%s, %s, %s)",
                    (owner_id, tenant_id, owner),
                )
            cur.execute(
                "INSERT INTO baidu_accounts (id, tenant_id, alias, owner_user_id, user_name)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (tenant_id, alias) DO UPDATE"
                " SET owner_user_id = EXCLUDED.owner_user_id,"
                "     user_name = EXCLUDED.user_name, updated_at = now()",
                (str(uuid.uuid4()), tenant_id, alias, owner_id, str(item.get("user_name") or "")),
            )

        for item in grants:
            sub = str(item.get("sub") or "").strip()
            alias = str(item.get("alias") or "").strip().lower()
            permission = str(item.get("permission") or "").strip().lower()
            if not sub or not alias or permission not in ("read", "write"):
                continue
            cur.execute(
                "SELECT id FROM users WHERE tenant_id = %s AND idp_subject = %s",
                (tenant_id, sub),
            )
            row = cur.fetchone()
            if row:
                user_id = row[0]
            else:
                user_id = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO users (id, tenant_id, idp_subject) VALUES (%s, %s, %s)",
                    (user_id, tenant_id, sub),
                )
            cur.execute(
                "SELECT id FROM baidu_accounts WHERE tenant_id = %s AND alias = %s",
                (tenant_id, alias),
            )
            row = cur.fetchone()
            if not row:
                print(f"跳过 grant：账户 {alias} 未导入", file=sys.stderr)
                continue
            cur.execute(
                "INSERT INTO account_grants (user_id, account_id, permission)"
                " VALUES (%s, %s, %s)"
                " ON CONFLICT (user_id, account_id) DO UPDATE SET permission = EXCLUDED.permission",
                (user_id, row[0], permission),
            )

        conn.commit()

        # 导入后自检：账户数、owner 完整性、grant 数
        cur.execute(
            "SELECT count(*) FROM baidu_accounts"
            " WHERE tenant_id = %s AND owner_user_id IS NOT NULL",
            (tenant_id,),
        )
        summary["accounts_with_owner"] = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM account_grants g JOIN baidu_accounts a ON a.id = g.account_id"
            " WHERE a.tenant_id = %s",
            (tenant_id,),
        )
        summary["grants_in_db"] = cur.fetchone()[0]

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入文件式授权表到 PostgreSQL")
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--tenant", default="t_default")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    summary = import_data(
        args.dsn, load(args.file), tenant_id=args.tenant, dry_run=args.dry_run
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not args.dry_run and summary.get("accounts_with_owner") != summary["accounts"]:
        print("账户 owner 数量与账户数不一致，请检查！", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
