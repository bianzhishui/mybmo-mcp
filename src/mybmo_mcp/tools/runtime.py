"""运行期依赖装配。

工具函数不自己 new 客户端、不自己读环境变量，统一从这里取。这样测试可以
``set_runtime()`` 注入假实现，不用去 mock 一堆全局状态。

主密钥只在**真正需要凭据**时才要求存在：``search_capabilities`` 这类纯检索
不需要密钥，没有配置也应当能用。

身份有三个来源，优先级从高到低：

1. SDK 的鉴权上下文（``get_access_token``）——HTTP 模式下由 SDK 中间件写入
2. ``Runtime.principal``——测试或单进程脚本显式注入
3. 本地隐式身份——**仅当** ``MYBMO_AUTH_MODE=local``

第 3 条绝不能与多用户模式共用：进程级单例装不下每个请求的身份，回退成"全权"
就是越权。所以非 local 模式下没有前两级身份时直接报错。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import httpx2
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from mybmo_mcp.audit.log import AuditLog
from mybmo_mcp.auth.access import AccountAccess
from mybmo_mcp.auth.binding import BindingManager, StateStore
from mybmo_mcp.auth.oauth import BaiduOAuth
from mybmo_mcp.auth.registry import AccountRegistry
from mybmo_mcp.auth.resource import SCOPE_READ, JwtVerifier
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
from mybmo_mcp.baidu.errors import CredentialError
from mybmo_mcp.baidu.transport import BaiduTransport, RequestCredentials
from mybmo_mcp.config import Settings, get_settings

_runtime: Runtime | None = None


def _principal_from_bearer_token() -> tuple[str, str, str] | None:
    """从 SDK 的鉴权上下文里取 ``(sub, 组织, 应用)``。

    SDK 的 ``AuthContextMiddleware`` 把已验证的 ``AccessToken`` 放进 ContextVar，
    这里只读它——**不重新解析 JWT**。令牌在中间件里已经验过签名、iss、aud、exp，
    再解析一遍只会引入"两处校验规则不一致"的风险。

    组织取 Casdoor 令牌的 ``owner``：built-in 组织只服务 dbx-mcp / casdoor-mcp，
    pandabase 组织只服务 mybmo-mcp，授权表按它隔离。应用取 ``azp``（client_id），
    用于 grant 里写了 ``app`` 时的再收窄。
    """
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
    except ImportError:  # SDK 版本变化时不影响 local 模式
        return None
    access = get_access_token()
    if access is None or not access.subject:
        return None
    claims = getattr(access, "claims", None) or {}
    org = str(claims.get("owner") or "").strip()
    app = str(getattr(access, "client_id", "") or claims.get("azp") or "").strip()
    return str(access.subject), org, app


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
    def binding(self) -> BindingManager:
        """百度账户绑定：签发授权链接 + 处理回调。

        只有 ``auth bind`` 与 ``/oauth/baidu/callback`` 用它，所以缺配置时**不在这里失败**，
        而是在被用到时才报错——检索能力不需要回调地址。
        """
        callback = self.settings.mybmo_baidu_callback_url.strip()
        if not callback:
            raise CredentialError(
                "缺少 MYBMO_BAIDU_CALLBACK_URL（在 dev2 应用「安全设置」里登记的授权回调地址），"
                "没有它生成不了授权链接。取值见 docs/auth-contract.md"
            )
        if not self.settings.baidu_app_id or not self.settings.baidu_secret_key:
            raise CredentialError("缺少 BAIDU_APP_ID / BAIDU_SECRET_KEY，无法绑定百度账户")
        return BindingManager(
            self.oauth,
            self.vault,
            StateStore(self.settings.mybmo_data_dir),
            app_id=self.settings.baidu_app_id,
            secret_key=self.settings.baidu_secret_key,
            redirect_uri=callback,
        )

    @cached_property
    def capabilities(self) -> CapabilityRegistry:
        """能力注册表。389 条能力 + 585 个模型，进程内只加载一次。"""
        return CapabilityRegistry.load()

    @cached_property
    def audit(self) -> AuditLog:
        return AuditLog(self.settings.mybmo_data_dir / "audit")

    @cached_property
    def client(self) -> httpx2.AsyncClient:
        """共享 HTTP 连接池。传输层不持有它，因此不会在每次调用后关闭。"""
        return httpx2.AsyncClient(timeout=45.0)

    # ---------- 身份 ----------

    def current_principal(self) -> Principal:
        """定出这次调用的身份。三级来源，优先级递减。

        最后一级只在 ``MYBMO_AUTH_MODE=local`` 才允许——多用户下回退成"本机就是管理员"
        等于全量越权，所以那条路径是显式报错而不是默认放行。
        """
        if self.principal is not None:
            return self.principal

        bearer = _principal_from_bearer_token()
        if bearer is not None:
            sub, org, app = bearer
            return self.tenancy.resolve_principal(sub, org=org, app=app)

        if self.settings.mybmo_auth_mode != MODE_LOCAL:
            raise TenancyError(
                f"当前处于多用户模式（mode={self.settings.mybmo_auth_mode}），"
                "但这次调用没有携带身份。请求应经过鉴权中间件，"
                "不能回退成本地隐式身份——那等于任何人都是管理员"
            )
        return Principal.local_admin()

    @cached_property
    def verifier(self) -> JwtVerifier | None:
        """多用户模式才有校验器。local 模式返回 ``None``，SDK 也就不装鉴权中间件。

        ``audience`` 取 ``MYBMO_OIDC_AUDIENCE`` 而不是资源地址——IdP 把 ``aud``
        填成 client_id，见 docs/auth-contract.md。可以填多个（逗号分隔），每个显式注册的客户端一个；
        不配就拒绝启动。
        """
        if self.settings.mybmo_auth_mode == MODE_LOCAL:
            return None
        if not self.settings.mybmo_oidc_audience:
            raise TenancyError(
                "多用户模式缺少 MYBMO_OIDC_AUDIENCE。它是 IdP 里本服务应用的 "
                "clientId，不是服务地址——aud 校验对象是「允许调用本服务的客户端」，"
                "理由见 docs/auth-contract.md"
            )
        return JwtVerifier(
            issuer=self.settings.casdoor_issuer,
            audience=self.settings.mybmo_oidc_audience,
            jwks_url=self.settings.mybmo_jwks_url or None,
        )

    def auth_settings(self) -> AuthSettings | None:
        """给 ``MCPServer`` 用的 Resource Server 配置。

        SDK 会据此挂载 ``/.well-known/oauth-protected-resource`` 与 401 + ``WWW-Authenticate``，
        不需要我们再写一遍中间件。

        ``validate_token_resource=False`` 是必须的：SDK 这个开关要求令牌带
        RFC 8707 的 ``resource`` 字段等于 ``resource_server_url``，
        而 Casdoor 不签发 ``resource``（它只填 ``aud``）。设成 ``True`` 会拒掉所有令牌。
        audience 已由 verifier 自己校验。
        """
        if self.verifier is None:
            return None
        if not self.settings.mybmo_resource_url:
            raise TenancyError("多用户模式缺少 MYBMO_RESOURCE_URL（元数据里的 resource 字段）")
        return AuthSettings(
            issuer_url=AnyHttpUrl(self.verifier.issuer),
            resource_server_url=AnyHttpUrl(self.settings.mybmo_resource_url),
            required_scopes=[SCOPE_READ],
            validate_token_resource=False,
        )

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
