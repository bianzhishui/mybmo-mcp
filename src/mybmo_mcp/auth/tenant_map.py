"""租户映射：Casdoor ``client_id`` → 租户，并在首次见到时**自动开户**。

背景（docs/decisions/0008-tenant-per-client.md）：

* 一个租户对应一个 client_id（库里是 ``tenants.client_id`` + 唯一索引，1:1）
* 准入闸门只剩**组织白名单**（``MYBMO_OIDC_ORGS``）：pandabase 组织内任何 Casdoor 应用
  拿着自己用户的令牌就能自助开一个租户——这是明确的取舍，不是漏掉的校验
* 想收紧就配 ``MYBMO_OIDC_AUDIENCE``：设了就额外要求令牌 aud 命中白名单

自动开户的 id 与名字都由这里生成：``id = t_<12hex>``、``name = 租户-<id 后 4 位>``。
名字只是给人看的，随时可以用 ``tenant rename`` 改。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from mybmo_mcp.baidu.errors import CredentialError

TENANT_PREFIX = "t_"
NAME_PREFIX = "租户-"
_ID_HEX_LEN = 12


@dataclass(frozen=True, slots=True)
class TenantRef:
    """租户的一行。``client_id`` 为空说明还没绑定（启动自检会报出来）。"""

    id: str
    name: str
    client_id: str = ""
    users: int = 0
    accounts: int = 0

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "client_id": self.client_id,
            "users": self.users,
            "accounts": self.accounts,
        }


def new_tenant_id() -> str:
    return f"{TENANT_PREFIX}{uuid.uuid4().hex[:_ID_HEX_LEN]}"


def default_tenant_name(tenant_id: str) -> str:
    """自动命名：``租户-a3f2``。

    不用 client_id 当名字——32 位十六进制没人记得住，排查时要靠猜。
    也不从令牌里取：JWT 的 ``name`` 是**用户**的显示名，不是应用名。
    """
    return f"{NAME_PREFIX}{tenant_id[-4:]}"


class PgTenantMap:
    """``tenants.client_id`` 的读写。PG 专用（file 后端没有租户维度）。"""

    def __init__(
        self,
        dsn: str,
        *,
        connect: Callable[[], Any] | None = None,
    ) -> None:
        if not dsn:
            raise CredentialError("PgTenantMap 需要 MYBMO_DATABASE_URL")
        self._dsn = dsn
        self._connect_override = connect

    # ---------- 连接 ----------

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        if self._connect_override is not None:
            conn = self._connect_override()
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
            return

        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            yield conn

    # ---------- 解析 ----------

    def resolve(self, client_id: str) -> str:
        """client_id → 租户 id。没见过的 client_id **自动开户**。

        并发首请求可能同时走到这里，所以用 ``ON CONFLICT (client_id) DO NOTHING``
        再回查，保证只建一条、两边拿到同一个 id。
        """
        client_id = (client_id or "").strip()
        if not client_id:
            raise CredentialError(
                "令牌里没有 client_id（azp / aud），无法判断属于哪个租户"
            )
        with self._conn() as conn:
            row = conn.execute(
                "SELECT tenant_key FROM tenants WHERE client_id = %s", (client_id,)
            ).fetchone()
            if row is not None:
                return str(row["tenant_key"])

            tenant_key = new_tenant_id()
            conn.execute(
                "INSERT INTO tenants (tenant_key, name, client_id) VALUES (%s, %s, %s)"
                " ON CONFLICT (client_id) DO NOTHING",
                (tenant_key, default_tenant_name(tenant_key), client_id),
            )
            row = conn.execute(
                "SELECT tenant_key FROM tenants WHERE client_id = %s", (client_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - 理论上不可达：插入被冲突时回查必命中
            raise CredentialError(f"自动开户失败：client_id={client_id}")
        return str(row["tenant_key"])

    def get(self, tenant_id: str) -> TenantRef | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT t.tenant_key, t.name, coalesce(t.client_id, '') AS client_id,"
                "  (SELECT count(*) FROM users u WHERE u.tenant_id = t.tenant_key) AS users,"
                "  (SELECT count(*) FROM baidu_accounts b"
                "     WHERE b.tenant_id = t.tenant_key) AS accounts"
                " FROM tenants t WHERE t.tenant_key = %s",
                (tenant_id,),
            ).fetchone()
        if row is None:
            return None
        return TenantRef(
            id=str(row["tenant_key"]),
            name=str(row["name"]),
            client_id=str(row["client_id"]),
            users=int(row["users"]),
            accounts=int(row["accounts"]),
        )

    def list(self) -> list[TenantRef]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT t.tenant_key, t.name, coalesce(t.client_id, '') AS client_id,"
                "  (SELECT count(*) FROM users u WHERE u.tenant_id = t.tenant_key) AS users,"
                "  (SELECT count(*) FROM baidu_accounts b"
                "     WHERE b.tenant_id = t.tenant_key) AS accounts"
                " FROM tenants t ORDER BY t.created_at, t.tenant_key"
            ).fetchall()
        return [
            TenantRef(
                id=str(r["tenant_key"]),
                name=str(r["name"]),
                client_id=str(r["client_id"]),
                users=int(r["users"]),
                accounts=int(r["accounts"]),
            )
            for r in rows
        ]

    # ---------- 维护 ----------

    def set_client(self, tenant_id: str, client_id: str) -> None:
        """绑定/修复 client_id。冲突（同一个 client 已属别的租户）要能看懂。"""
        client_id = (client_id or "").strip()
        if not client_id:
            raise CredentialError("client_id 不能为空")
        with self._conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM tenants WHERE tenant_key = %s", (tenant_id,)
            ).fetchone()
            if exists is None:
                raise CredentialError(f"租户不存在：{tenant_id}")
            try:
                conn.execute(
                    "UPDATE tenants SET client_id = %s WHERE tenant_key = %s",
                    (client_id, tenant_id),
                )
            except Exception as exc:  # 唯一约束冲突
                if type(exc).__name__ == "UniqueViolation":
                    # 事务已被置为 aborted，必须先回滚才能继续查
                    conn.rollback()
                    owner = conn.execute(
                        "SELECT tenant_key FROM tenants WHERE client_id = %s", (client_id,)
                    ).fetchone()
                    bound = owner["tenant_key"] if owner else "(未知)"
                    raise CredentialError(
                        f"client_id {client_id} 已绑定到租户 {bound}；"
                        "一个 client 只能属于一个租户"
                    ) from exc
                raise

    def rename(self, tenant_id: str, name: str) -> None:
        name = (name or "").strip()
        if not name:
            raise CredentialError("租户名不能为空")
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE tenants SET name = %s WHERE tenant_key = %s", (name, tenant_id)
            )
            if cur.rowcount == 0:
                raise CredentialError(f"租户不存在：{tenant_id}")

    def stats(self) -> dict[str, int]:
        """探活用计数：租户 / 用户 / 账户 / 分享。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT (SELECT count(*) FROM tenants) AS tenants,"
                " (SELECT count(*) FROM users) AS users,"
                " (SELECT count(*) FROM baidu_accounts) AS accounts,"
                " (SELECT count(*) FROM account_grants) AS grants"
            ).fetchone()
        return {k: int(row[k]) for k in ("tenants", "users", "accounts", "grants")}

    def unbound(self) -> list[str]:
        """还没绑 client_id 的租户 id。启动自检用它。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT tenant_key FROM tenants"
                " WHERE client_id IS NULL OR client_id = '' ORDER BY tenant_key"
            ).fetchall()
        return [str(r["tenant_key"]) for r in rows]
