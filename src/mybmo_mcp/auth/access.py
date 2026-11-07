"""账户取用：把「凭据库里有什么」和「这个身份能用什么」合成一条路径。

工具层不应该自己 ``registry.get(alias)``——那样会绕过授权。所有取账户的调用都必须
经过这里，顺序是固定的：

1. 先从凭据库列出全部账户（可能抛凭据损坏错误）
2. 再按身份筛出可见的
3. 最后按所需档位判定

先筛后判，而不是先判后筛：如果先按 alias 取凭据再看权限，"账户不存在"与"无权"
就会给出不同错误，等于把别人的账户名变成可探测的。
"""

from __future__ import annotations

from mybmo_mcp.auth.registry import AccountEntry, AccountRegistry
from mybmo_mcp.auth.tenancy import (
    PERMISSION_READ,
    AccessDenied,
    Principal,
    TenancyStore,
)
from mybmo_mcp.baidu.errors import CredentialError


class AccountAccess:
    """按身份取账户。无状态，可安全共享。"""

    def __init__(self, registry: AccountRegistry, tenancy: TenancyStore) -> None:
        self._registry = registry
        self._tenancy = tenancy

    @property
    def tenancy(self) -> TenancyStore:
        return self._tenancy

    def entries(self, principal: Principal) -> list[AccountEntry]:
        """当前身份可见的账户列表。"""
        entries = self._registry.list_accounts()
        visible = set(self._tenancy.visible(principal, [entry.alias for entry in entries]))
        return [entry for entry in entries if entry.alias in visible]

    def resolve(
        self,
        principal: Principal,
        requested: str | None = None,
        *,
        permission: str = PERMISSION_READ,
    ) -> AccountEntry:
        """定出这次调用用哪个账户，并校验档位。

        ``requested`` 为空时只在**当前身份唯一可用**的账户上生效。有多个候选就报错，
        不悄悄用默认账户——默认账户是进程级的，多用户下极可能属于别人。
        """
        entries = self.entries(principal)
        if not entries:
            raise CredentialError(
                f"当前身份（{principal.sub}）没有可用的百度账户。"
                "先完成一次授权绑定，或让账户管理员在 tenancy.json 里授权"
            )

        if requested is None:
            if len(entries) > 1:
                aliases = ", ".join(entry.alias for entry in entries)
                raise CredentialError(
                    f"当前身份可用 {len(entries)} 个账户，必须显式指定 alias：{aliases}"
                )
            entry = entries[0]
        else:
            matched = [entry for entry in entries if entry.alias == requested]
            if not matched:
                aliases = ", ".join(entry.alias for entry in entries)
                raise AccessDenied(
                    f"当前身份（{principal.sub}）不能使用账户 {requested}；可用的只有：{aliases}",
                    sub=principal.sub,
                    alias=requested,
                    required=permission,
                )
            entry = matched[0]

        self._tenancy.require(principal, entry.alias, permission)
        return entry
