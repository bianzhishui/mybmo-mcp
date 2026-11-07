"""运行期依赖装配。

工具函数不自己 new 客户端、不自己读环境变量，统一从这里取。这样测试可以
``set_runtime()`` 注入假实现，不用去 mock 一堆全局状态。

主密钥只在**真正需要凭据**时才要求存在：``search_capabilities`` 这类纯检索
不需要密钥，没有配置也应当能用。

身份有三级来源，优先级从高到低：

1. 请求级上下文（``set_current_principal``）——HTTP 模式下由 ASGI 中间件按 JWT 写入
2. ``Runtime.principal``——测试或单进程脚本显式注入
3. 本地隐式身份——**仅当** ``MYBMO_AUTH_MODE=local``

第 3 条绝不能与多用户模式共用：进程级单例装不下每个请求的身份，回退成"全权"
就是越权。所以非 local 模式下没有前两级身份时直接报错。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from functools import cached_property

import httpx2

from mybmo_mcp.auth.access import AccountAccess
from mybmo_mcp.auth.oauth import BaiduOAuth
from mybmo_mcp.auth.registry import AccountRegistry
from mybmo_mcp.auth.service import CredentialService
from mybmo_mcp.auth.tenancy import (
    MODE_LOCAL,
    PERMISSION_READ,
    Principal,
    TenancyError,
    TenancyStore,
)
from mybmo_mcp.auth.vault import FileVault
from mybmo_mcp.baidu.capabilities import CapabilityRegistry
from mybmo_mcp.baidu.transport import BaiduTransport, RequestCredentials
from mybmo_mcp.config import Settings, get_settings

_runtime: Runtime | None = None

#: 请求级身份。HTTP 模式下每个请求各写各的，不会互相污染。
_current_principal: ContextVar[Principal | None] = ContextVar("mybmo_principal", default=None)


def set_current_principal(principal: Principal | None) -> Token[Principal | None]:
    """为当前上下文绑定身份（P4 的中间件与测试用）。用完应 ``reset``。"""
    return _current_principal.set(principal)


def reset_current_principal(token: Token[Principal | None]) -> None:
    _current_principal.reset(token)


class _AccountCredentials:
    """把「身份 + alias + 所需档位」适配成传输层要的 ``CredentialSource``。

    授权判定发生在 ``credentials()`` 里，也就是**发请求之前**：
    越权的调用永远不会产生网络请求。
    """

    def __init__(
        self,
        service: CredentialService,
        access: AccountAccess,
        principal: Principal,
        alias: str | None,
        permission: str,
    ) -> None:
        self._service = service
        self._access = access
        self._principal = principal
        self._alias = alias
        self._permission = permission
        self._resolved = ""

    @property
    def alias(self) -> str:
        """已解析的 alias。构造时还不知道，第一次取凭据后才有值。"""
        return self._resolved

    async def credentials(self) -> RequestCredentials:
        entry = self._access.resolve(self._principal, self._alias, permission=self._permission)
        self._resolved = entry.alias
        return await self._service.credentials_for(entry.alias)


@dataclass
class Runtime:
    """一次进程生命周期内的依赖集合。组件按需构造，避免缺少配置时整个服务起不来。"""

    settings: Settings

    #: 测试或多用户改造前的显式身份；``None`` 表示按模式推断
    principal: Principal | None = None

    @cached_property
    def vault(self) -> FileVault:
        return FileVault(self.settings.mybmo_data_dir, self.settings.master_key_bytes())

    @cached_property
    def registry(self) -> AccountRegistry:
        return AccountRegistry(self.vault)

    @cached_property
    def tenancy(self) -> TenancyStore:
        return TenancyStore(
            self.settings.mybmo_data_dir / "tenancy.json", mode=self.settings.mybmo_auth_mode
        )

    @cached_property
    def access(self) -> AccountAccess:
        return AccountAccess(self.registry, self.tenancy)

    @cached_property
    def oauth(self) -> BaiduOAuth:
        return BaiduOAuth(self.settings.baidu_app_id, self.settings.baidu_secret_key)

    @cached_property
    def credentials(self) -> CredentialService:
        return CredentialService(
            self.registry, self.oauth, lock_dir=self.settings.mybmo_data_dir / "locks"
        )

    @cached_property
    def capabilities(self) -> CapabilityRegistry:
        """能力注册表。389 条能力 + 585 个模型，进程内只加载一次。"""
        return CapabilityRegistry.load()

    @cached_property
    def client(self) -> httpx2.AsyncClient:
        """共享 HTTP 连接池。传输层不持有它，因此不会在每次调用后关闭。"""
        return httpx2.AsyncClient(timeout=45.0)

    # ---------- 身份 ----------

    def current_principal(self) -> Principal:
        scoped = _current_principal.get()
        if scoped is not None:
            return scoped
        if self.principal is not None:
            return self.principal
        if self.settings.mybmo_auth_mode != MODE_LOCAL:
            raise TenancyError(
                f"当前处于多用户模式（mode={self.settings.mybmo_auth_mode}），"
                "但这次调用没有携带身份。请求应经过鉴权中间件，"
                "不能回退成本地隐式身份——那等于任何人都是管理员"
            )
        return Principal.local_admin()

    # ---------- 取用 ----------

    def transport_for(
        self,
        alias: str | None = None,
        *,
        principal: Principal | None = None,
        permission: str = PERMISSION_READ,
    ) -> BaiduTransport:
        source = _AccountCredentials(
            self.credentials,
            self.access,
            principal if principal is not None else self.current_principal(),
            alias,
            permission,
        )
        return BaiduTransport(source, client=self.client)


def build_runtime(settings: Settings | None = None) -> Runtime:
    return Runtime(settings=settings or get_settings())


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = build_runtime()
    return _runtime


def set_runtime(runtime: Runtime | None) -> None:
    """测试用：注入或清空运行期依赖。"""
    global _runtime
    _runtime = runtime
