"""一个租户的运行期依赖集合。

为什么需要它：凭据库、授权表、应用密钥登记都是**按租户实例化**的（库里所有查询带
``tenant_id``）。以前它们是进程级单例 + 一个全局 ``MYBMO_TENANT_ID``，等于"只有一个租户"。
现在租户由令牌里的 ``client_id`` 解析出来（ADR 0008），同一个进程要同时服务多个租户，
所以每个租户一套实例，按 ``tenant_id`` 缓存。

**刻意不做成隐式上下文**（不用 contextvar）：漏传租户就会写成跨租户读写，而这类 bug
不会立刻报错、只会静默污染数据。显式传一个 ``TenantContext``，漏了就是 ``AttributeError``。
"""

from __future__ import annotations

from dataclasses import dataclass

from mybmo_mcp.auth.access import AccountAccess
from mybmo_mcp.auth.app_secrets import AppSecretStore
from mybmo_mcp.auth.registry import AccountRegistry
from mybmo_mcp.auth.service import CredentialService
from mybmo_mcp.auth.store import CredentialStore
from mybmo_mcp.auth.tenancy import TenancyStore


@dataclass(frozen=True, slots=True)
class TenantContext:
    """某个租户的存储与判权组件。由 :meth:`Runtime.context` 构造并缓存。"""

    tenant_id: str
    store: CredentialStore
    tenancy: TenancyStore
    app_secrets: AppSecretStore
    registry: AccountRegistry
    access: AccountAccess
    credentials: CredentialService
