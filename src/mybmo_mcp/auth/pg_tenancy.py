"""PostgreSQL 版授权存储。

与 :class:`mybmo_mcp.auth.tenancy.TenancyStore` **同接口**，调用方零改动；
区别是数据在库里、隔离靠 SQL 的 ``tenant_id`` 条件，而不是"整文件读写"。

设计约束（见 ``docs/architecture.md`` 第四节与 ``migrations/001_init.sql``）：

* 账户私有：``baidu_accounts.owner_user_id`` 是绑定者，owner 隐式 admin
* ``account_grants`` 只表达 ``read`` / ``write`` —— ``admin`` 不可转授
* 所有查询都带 ``tenant_id``；不把裸 SQL 暴露给上层
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from mybmo_mcp.auth.tenancy import (
    _RANK,
    MODE_OIDC,
    PERMISSION_ADMIN,
    PERMISSION_READ,
    PERMISSION_WRITE,
    AccessDenied,
    Account,
    Principal,
    TenancyError,
    satisfies,
    validate_alias,
)

DEFAULT_TENANT = "t_default"


class PgTenancyStore:
    """授权表存在 PostgreSQL 里的实现。"""

    def __init__(
        self,
        dsn: str,
        *,
        tenant_id: str = DEFAULT_TENANT,
        mode: str = MODE_OIDC,
        connect: Callable[[], Any] | None = None,
    ) -> None:
        if not dsn:
            raise TenancyError("PgTenancyStore 需要 MYBMO_DATABASE_URL")
        self._dsn = dsn
        self._tenant_id = tenant_id
        self._mode = mode
        self._connect_override = connect

    # ---------- 连接 ----------

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        if self._connect_override is not None:
            conn = self._connect_override()
            try:
                yield conn
            finally:
                conn.close()
            return

        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            yield conn

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    def reload(self) -> None:
        """库版没有缓存，保留方法只为接口一致。"""

    # ---------- 身份 ----------

    def resolve_principal(self, sub: str, *, org: str = "", app: str = "") -> Principal:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT idp_subject, display_name FROM users"
                " WHERE tenant_id = %s AND idp_subject = %s",
                (self._tenant_id, sub),
            )
            row = cur.fetchone()
        if row is None:
            # 未登记不自动创建：登录成功但没有任何账户，是可解释的状态
            return Principal(sub=sub, org=org, app=app, tenant_id="", source=self._mode)
        return Principal(
            sub=row["idp_subject"],
            tenant_id=self._tenant_id,
            display_name=row["display_name"] or "",
            source=self._mode,
            org=org,
            app=app,
        )

    def _user_id(self, cur: Any, sub: str, *, display_name: str = "") -> str:
        """取（必要时创建）用户行，返回其内部 id。"""
        cur.execute(
            "SELECT id FROM users WHERE tenant_id = %s AND idp_subject = %s",
            (self._tenant_id, sub),
        )
        row = cur.fetchone()
        if row is not None:
            return row["id"]
        user_id = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO users (id, tenant_id, idp_subject, display_name)"
            " VALUES (%s, %s, %s, %s)",
            (user_id, self._tenant_id, sub, display_name),
        )
        return user_id

    def _account_row(self, cur: Any, alias: str) -> Any:
        cur.execute(
            "SELECT a.id, a.alias, a.user_name, u.idp_subject AS owner"
            " FROM baidu_accounts a JOIN users u ON u.id = a.owner_user_id"
            " WHERE a.tenant_id = %s AND a.alias = %s",
            (self._tenant_id, alias),
        )
        return cur.fetchone()

    # ---------- 查询 ----------

    def permission_for(self, principal: Principal, alias: str) -> str | None:
        if principal.unrestricted:
            return PERMISSION_ADMIN
        with self._conn() as conn, conn.cursor() as cur:
            row = self._account_row(cur, alias)
            if row is None:
                return None
            if row["owner"] == principal.sub:
                return PERMISSION_ADMIN
            cur.execute(
                "SELECT g.permission FROM account_grants g"
                " JOIN users u ON u.id = g.user_id"
                " JOIN baidu_accounts a ON a.id = g.account_id"
                " WHERE a.tenant_id = %s AND a.alias = %s AND u.idp_subject = %s",
                (self._tenant_id, alias, principal.sub),
            )
            grants = [r["permission"] for r in cur.fetchall()]
        if not grants:
            return None
        return max(grants, key=lambda p: _RANK.get(p, -1))

    def visible(self, principal: Principal, aliases: list[str]) -> list[str]:
        if principal.unrestricted:
            return list(aliases)
        return [alias for alias in aliases if self.permission_for(principal, alias) is not None]

    def owner_of(self, alias: str) -> str | None:
        with self._conn() as conn, conn.cursor() as cur:
            row = self._account_row(cur, alias)
        return row["owner"] if row else None

    def accounts(self) -> list[Account]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT a.alias, a.user_name, u.idp_subject AS owner"
                " FROM baidu_accounts a JOIN users u ON u.id = a.owner_user_id"
                " WHERE a.tenant_id = %s ORDER BY a.alias",
                (self._tenant_id,),
            )
            rows = cur.fetchall()
        return [
            Account(alias=r["alias"], owner=r["owner"], user_name=r["user_name"] or "")
            for r in rows
        ]

    def shares_of(self, alias: str) -> dict[str, str]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT u.idp_subject, g.permission FROM account_grants g"
                " JOIN users u ON u.id = g.user_id"
                " JOIN baidu_accounts a ON a.id = g.account_id"
                " WHERE a.tenant_id = %s AND a.alias = %s",
                (self._tenant_id, alias),
            )
            return {r["idp_subject"]: r["permission"] for r in cur.fetchall()}

    def stats(self) -> dict[str, int]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM users WHERE tenant_id = %s", (self._tenant_id,))
            users = cur.fetchone()["n"]
            cur.execute(
                "SELECT count(*) AS n FROM baidu_accounts WHERE tenant_id = %s", (self._tenant_id,)
            )
            accounts = cur.fetchone()["n"]
            cur.execute(
                "SELECT count(*) AS n FROM account_grants g"
                " JOIN baidu_accounts a ON a.id = g.account_id WHERE a.tenant_id = %s",
                (self._tenant_id,),
            )
            grants = cur.fetchone()["n"]
        return {
            "tenants": 1 if users or accounts else 0,
            "users": users,
            "accounts": accounts,
            "grants": grants,
        }

    # ---------- 写入 ----------

    def register_account(
        self, alias: str, *, owner: str, user_name: str = "", note: str = ""
    ) -> None:
        alias = validate_alias(alias)
        if not owner:
            raise TenancyError("登记账户缺 owner：账户必须归某个 sub 所有")
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenants (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                (self._tenant_id, self._tenant_id),
            )
            owner_id = self._user_id(cur, owner)
            cur.execute(
                "INSERT INTO baidu_accounts (id, tenant_id, alias, owner_user_id, user_name)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (tenant_id, alias) DO UPDATE"
                " SET owner_user_id = EXCLUDED.owner_user_id,"
                "     user_name = EXCLUDED.user_name, updated_at = now()",
                (str(uuid.uuid4()), self._tenant_id, alias, owner_id, user_name),
            )
            conn.commit()

    def share(self, alias: str, *, sub: str, permission: str) -> None:
        permission = permission.strip().lower()
        if permission not in (PERMISSION_READ, PERMISSION_WRITE):
            raise TenancyError("分享只能给 read 或 write；admin 是 owner 的天然权利")
        alias = validate_alias(alias)
        with self._conn() as conn, conn.cursor() as cur:
            row = self._account_row(cur, alias)
            if row is None:
                raise TenancyError(f"账户 {alias} 未登记，不能分享")
            user_id = self._user_id(cur, sub)
            cur.execute(
                "INSERT INTO account_grants (user_id, account_id, permission)"
                " VALUES (%s, %s, %s)"
                " ON CONFLICT (user_id, account_id) DO UPDATE SET permission = EXCLUDED.permission",
                (user_id, row["id"], permission),
            )
            conn.commit()

    def revoke(self, alias: str, *, sub: str) -> bool:
        alias = validate_alias(alias)
        with self._conn() as conn, conn.cursor() as cur:
            row = self._account_row(cur, alias)
            if row is None:
                return False
            cur.execute(
                "DELETE FROM account_grants g USING users u"
                " WHERE g.user_id = u.id AND g.account_id = %s"
                "   AND u.tenant_id = %s AND u.idp_subject = %s",
                (row["id"], self._tenant_id, sub),
            )
            removed = cur.rowcount > 0
            conn.commit()
        return removed

    # ---------- 判权 ----------

    def require(self, principal: Principal, alias: str, permission: str) -> None:
        granted = self.permission_for(principal, alias)
        if granted is None:
            owner = self.owner_of(alias)
            reason = (
                f"该账户属于别人（owner={owner}），需要 owner 给你授权"
                if owner is not None
                else "授权表里没有这个 alias 的归属记录，账户不可见"
            )
            raise AccessDenied(
                f"当前身份（{principal.sub}）没有账户 {alias} 的授权；{reason}",
                sub=principal.sub,
                alias=alias,
                required=permission,
            )
        if not satisfies(granted, permission):
            raise AccessDenied(
                f"当前身份（{principal.sub}）对账户 {alias} 的权限是 {granted}，"
                f"本次操作需要 {permission}",
                sub=principal.sub,
                alias=alias,
                required=permission,
                granted=granted,
            )

    def describe(self, principal: Principal) -> dict[str, Any]:
        if principal.unrestricted:
            owned: dict[str, str] = {}
            grants: dict[str, str] = {}
        else:
            owned = {
                a.alias: PERMISSION_ADMIN for a in self.accounts() if a.owner == principal.sub
            }
            grants = {
                alias: permission
                for alias, permission in (
                    (a.alias, self.permission_for(principal, a.alias)) for a in self.accounts()
                )
                if permission is not None and alias not in owned
            }
        return {
            **principal.to_public(),
            "owned": owned,
            "grants": grants,
            "mode": self._mode,
            "account_model": "owned",
        }
