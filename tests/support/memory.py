"""测试用的内存实现：凭据、应用密钥、授权表、审计。

为什么放在 ``tests/`` 而不是 ``src/``：ADR 0009 之后运行期只有 PostgreSQL 一种后端。
如果把内存实现放进 ``src/``，它就变成了"第三个后端"，迟早会在生产被误用。
测试替身只需要行为一致，不需要持久化。
"""

from __future__ import annotations

from typing import Any

from mybmo_mcp.audit.base import build_record
from mybmo_mcp.auth.models import Credential, validate_alias
from mybmo_mcp.auth.tenancy import (
    _RANK,
    MODE_OIDC,
    PERMISSION_ADMIN,
    AccessDenied,
    Account,
    Grant,
    Principal,
    TenancyError,
    satisfies,
)
from mybmo_mcp.baidu.errors import CredentialError


class MemoryCredentialStore:
    """``CredentialStore`` 的内存实现。"""

    def __init__(self) -> None:
        self._items: dict[str, Credential] = {}
        self._default: str | None = None

    def save(self, credential: Credential) -> None:
        self._items[validate_alias(credential.alias)] = credential

    def load(self, alias: str) -> Credential:
        alias = validate_alias(alias)
        if alias not in self._items:
            raise CredentialError(f"账户不存在：{alias}")
        return self._items[alias]

    def delete(self, alias: str) -> None:
        self._items.pop(alias, None)
        if self._default == alias:
            self._default = None

    def aliases(self) -> list[str]:
        return sorted(self._items)

    def scan(self) -> tuple[list[str], list[tuple[Any, str]]]:
        return sorted(self._items), []

    def default_alias(self) -> str | None:
        return self._default

    def set_default(self, alias: str) -> None:
        if alias not in self._items:
            raise CredentialError(f"账户不存在，无法设为默认：{alias}")
        self._default = alias

    def locked(self, alias: str) -> Any:
        """内存版行锁：单进程测试里没有并发，直接交出可写句柄。"""
        from contextlib import contextmanager

        @contextmanager
        def _cm() -> Any:
            credential = self.load(alias)

            class _Handle:
                def __init__(self, current: Any) -> None:
                    self.credential = current

                def save(self, updated: Any) -> None:
                    self.credential = updated
                    self._store.save(updated)

            handle = _Handle(credential)
            handle._store = self
            yield handle

        return _cm()


class MemoryAppSecretStore:
    """``AppSecretStore`` 的内存实现。"""

    def __init__(self) -> None:
        self._items: dict[str, Any] = {}

    def save(self, app: Any) -> None:
        self._items[app.app_id] = app

    def get(self, app_id: str) -> Any:
        return self._items.get(app_id)

    def delete(self, app_id: str) -> None:
        self._items.pop(app_id, None)

    def list(self) -> list[Any]:
        return [self._items[key] for key in sorted(self._items)]


class MemoryTenancyStore:
    """``TenancyStore`` 的内存实现，语义与 PG 版一致（无持久化）。"""

    def __init__(self, *, mode: str = MODE_OIDC, users: dict[str, Principal] | None = None) -> None:
        self._mode = mode
        self._users: dict[str, Principal] = dict(users or {})
        self._accounts: dict[str, Account] = {}
        self._grants: list[Grant] = []

    # ---------- 元信息 ----------

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def tenant_id(self) -> str:
        return ""

    # ---------- 身份 ----------

    def resolve_principal(
        self, sub: str, *, org: str = "", app: str = "", fallback_name: str = ""
    ) -> Principal:
        user = self._users.get(sub)
        if user is None:
            return Principal(
                sub=sub, org=org, app=app, display_name=fallback_name,
                tenant_id="", source=self._mode,
            )
        return Principal(
            sub=user.sub,
            tenant_id=user.tenant_id,
            display_name=user.display_name or fallback_name,
            source=self._mode,
            org=org,
            app=app,
        )

    # ---------- 判权 ----------

    def permission_for(self, principal: Principal, alias: str) -> str | None:
        if principal.unrestricted:
            return PERMISSION_ADMIN
        account = self._accounts.get(alias)
        if account is not None and account.owner == principal.sub:
            return PERMISSION_ADMIN
        best: str | None = None
        for grant in self._grants:
            if grant.sub != principal.sub or grant.alias != alias:
                continue
            if best is None or _RANK[grant.permission] > _RANK[best]:
                best = grant.permission
        return best

    def visible(self, principal: Principal, aliases: list[str]) -> list[str]:
        if principal.unrestricted:
            return list(aliases)
        return [alias for alias in aliases if self.permission_for(principal, alias) is not None]

    def require(self, principal: Principal, alias: str, permission: str) -> None:
        granted = self.permission_for(principal, alias)
        if granted is None:
            account = self._accounts.get(alias)
            reason = (
                f"该账户属于别人（owner={account.owner}），需要 owner 给你授权"
                if account is not None
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

    # ---------- 维护 ----------

    def register_account(
        self, alias: str, *, owner: str, user_name: str = "", note: str = ""
    ) -> None:
        alias = validate_alias(alias)
        if not owner:
            raise TenancyError("登记账户缺 owner：账户必须归某个 sub 所有")
        self._accounts[alias] = Account(alias=alias, owner=owner, user_name=user_name, note=note)

    def unregister_account(self, alias: str) -> bool:
        removed = self._accounts.pop(alias, None) is not None
        before = len(self._grants)
        self._grants = [g for g in self._grants if g.alias != alias]
        return removed or before != len(self._grants)

    def share(self, alias: str, *, sub: str, permission: str) -> None:
        permission = (permission or "").strip().lower()
        if permission not in ("read", "write"):
            raise TenancyError("分享只能给 read 或 write；admin 是 owner 的天然权利")
        if alias not in self._accounts:
            raise TenancyError(f"账户未登记归属，无法分享：{alias}")
        self._grants = [g for g in self._grants if not (g.sub == sub and g.alias == alias)]
        self._grants.append(Grant(sub=sub, alias=alias, permission=permission))

    def revoke(self, alias: str, *, sub: str) -> bool:
        before = len(self._grants)
        self._grants = [g for g in self._grants if not (g.sub == sub and g.alias == alias)]
        return before != len(self._grants)

    # ---------- 查询 ----------

    def owner_of(self, alias: str) -> str | None:
        account = self._accounts.get(alias)
        return account.owner if account else None

    def accounts(self) -> list[Account]:
        return list(self._accounts.values())

    def shares_of(self, alias: str) -> dict[str, str]:
        return {g.sub: g.permission for g in self._grants if g.alias == alias}

    def describe(self, principal: Principal) -> dict[str, Any]:
        if principal.unrestricted:
            owned: dict[str, str] = {}
            grants: dict[str, str] = {}
        else:
            owned = {
                account.alias: PERMISSION_ADMIN
                for account in self._accounts.values()
                if account.owner == principal.sub
            }
            grants = {
                grant.alias: grant.permission
                for grant in self._grants
                if grant.sub == principal.sub and grant.alias not in owned
            }
        return {
            **principal.to_public(),
            "owned": owned,
            "grants": grants,
            "mode": self._mode,
            "account_model": "owned",
        }

    def stats(self) -> dict[str, int]:
        tenants = {p.tenant_id for p in self._users.values() if p.tenant_id}
        return {
            "tenants": len(tenants),
            "users": len(self._users),
            "accounts": len(self._accounts),
            "grants": len(self._grants),
        }


class MemoryAuditSink:
    """``AuditSink`` 的内存实现。

    复用 ``audit.base.build_record``——脱敏、payload_digest、字节数这些语义
    都在那里，测试替身不该另写一套（否则测的就不是生产行为了）。
    """

    def __init__(self, *, store_payload: bool = True) -> None:
        self.records: list[dict[str, Any]] = []
        self._store_payload = store_payload

    def record(self, **kwargs: Any) -> Any:
        record = build_record(store_payload=self._store_payload, **kwargs)
        self.records.append(record.to_dict())
        return record

    def rows(self) -> list[dict[str, Any]]:
        return list(self.records)

    def query(self, **kwargs: Any) -> list[dict[str, Any]]:
        rows = list(self.records)
        for key in ("tenant_id", "sub", "account", "state"):
            value = kwargs.get(key)
            if value:
                mapped = "sub" if key == "sub" else key
                rows = [r for r in rows if r.get(mapped) == value]
        limit = int(kwargs.get("limit") or 100)
        return rows[:limit]

# ---------------------------------------------------------------- 组装


class _FakeTenantMap:
    """``PgTenantMap`` 的替身：一个租户、固定 client_id。"""

    def __init__(self, tenancy: MemoryTenancyStore, tenant_id: str) -> None:
        self._tenancy = tenancy
        self._tenant_id = tenant_id
        self.client_id = "test-client"

    def resolve(self, client_id: str) -> str:
        if not client_id:
            raise CredentialError("令牌里没有 client_id")
        return self._tenant_id

    def get(self, tenant_id: str) -> Any:
        from mybmo_mcp.auth.tenant_map import TenantRef

        return TenantRef(id=tenant_id, name=tenant_id, client_id=self.client_id)

    def list(self) -> list[Any]:
        return [self.get(self._tenant_id)]

    def set_client(self, tenant_id: str, client_id: str) -> None:
        self.client_id = client_id

    def rename(self, tenant_id: str, name: str) -> None:
        return None

    def unbound(self) -> list[str]:
        return []

    def stats(self) -> dict[str, int]:
        return self._tenancy.stats()


def attach_memory_context(runtime: Any, tenant_id: str = "local") -> Any:
    """给一个真实 ``Runtime`` 装上内存上下文与内存租户映射。

    运行期只支持 PostgreSQL，所以测试不能真的调 ``_build_context``；
    这个 helper 把 ``_contexts`` 预填好，后面所有 ``runtime.context(...)`` 都用它。
    """
    from mybmo_mcp.auth.access import AccountAccess
    from mybmo_mcp.auth.registry import AccountRegistry
    from mybmo_mcp.auth.service import CredentialService
    from mybmo_mcp.auth.tenant_context import TenantContext

    store = MemoryCredentialStore()
    app_secrets = MemoryAppSecretStore()
    tenancy = MemoryTenancyStore()
    registry = AccountRegistry(store)
    ctx = TenantContext(
        tenant_id=tenant_id,
        store=store,
        tenancy=tenancy,
        app_secrets=app_secrets,
        registry=registry,
        access=AccountAccess(registry, tenancy),
        credentials=CredentialService(registry, None),
    )
    runtime._contexts[tenant_id] = ctx
    runtime.__dict__["tenant_map"] = _FakeTenantMap(tenancy, tenant_id)
    return ctx


class MemoryBindingStateStore:
    """``PgBindingStateStore`` 的内存替身：一次性 + TTL 语义一致。"""

    def __init__(self, *args: Any, ttl: float = 900.0, now: Any = None, **kwargs: Any) -> None:
        import time as _time

        self._items: dict[str, Any] = {}
        self._ttl = ttl
        self._now = now or _time.time

    def issue(self, *, subject: str, tenant: str, alias: str, app_id: str = "") -> Any:
        import secrets as _secrets

        from mybmo_mcp.auth.binding import BindingState
        from mybmo_mcp.auth.models import validate_alias

        alias = validate_alias(alias)
        if not subject:
            raise CredentialError("缺少 subject，无法签发绑定 state")
        now = self._now()
        state = BindingState(
            nonce=_secrets.token_urlsafe(32),
            subject=subject,
            tenant=tenant,
            alias=alias,
            issued_at=now,
            expires_at=now + self._ttl,
            app_id=app_id,
        )
        self._items[state.nonce] = state
        return state

    def consume(self, nonce: str) -> Any:
        state = self._items.pop(nonce, None)
        if state is None:
            return None
        if state.expires_at <= self._now():
            return None
        return state
