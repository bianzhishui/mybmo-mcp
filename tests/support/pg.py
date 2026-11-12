"""PG 集成测试的公共准备：一次跑完整套迁移。

为什么统一跑全量而不是各取所需：迁移 008（表规范）会重建所有表，
只跑子集建出来的 schema 与生产不一致，测出来的结论也就不可信。
"""

from __future__ import annotations

from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def all_migrations() -> list[str]:
    """按版本号顺序返回全部迁移 SQL。"""
    return [path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS_DIR.glob("0*.sql"))]


def seed_tenant(conn: object, tenant_key: str = "t_default") -> None:
    """建一条租户行。

    迁移 008 之后 credentials / app_secrets / binding_states 都外键到
    ``tenants(tenant_key)``，写这些表之前必须先有租户。
    """
    conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO tenants (tenant_key, name) VALUES (%s, %s)"
        " ON CONFLICT (tenant_key) DO NOTHING",
        (tenant_key, tenant_key),
    )
