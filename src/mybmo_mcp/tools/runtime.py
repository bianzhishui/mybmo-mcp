"""运行期依赖装配。

工具函数不自己 new 客户端、不自己读环境变量，统一从这里取。这样测试可以
``set_runtime()`` 注入假实现，不用去 mock 一堆全局状态。

主密钥只在**真正需要凭据**时才要求存在：``api_describe`` 这类纯目录查询
不需要密钥，没有配置也应当能用。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import httpx2

from mybmo_mcp.auth.oauth import BaiduOAuth
from mybmo_mcp.auth.registry import AccountRegistry
from mybmo_mcp.auth.service import CredentialService
from mybmo_mcp.auth.vault import FileVault
from mybmo_mcp.baidu.capabilities import CapabilityRegistry
from mybmo_mcp.baidu.transport import BaiduTransport, RequestCredentials
from mybmo_mcp.config import Settings, get_settings

_runtime: Runtime | None = None


class _AliasCredentials:
    """把一个 alias 适配成传输层要的 ``CredentialSource``。

    真正的刷新、加锁、落库都在 :class:`CredentialService` 里，
    传输层只看到「给我一对账户名与令牌」。
    """

    def __init__(self, service: CredentialService, alias: str | None) -> None:
        self._service = service
        self._alias = alias

    async def credentials(self) -> RequestCredentials:
        return await self._service.credentials_for(self._alias)


@dataclass
class Runtime:
    """一次进程生命周期内的依赖集合。组件按需构造，避免缺少配置时整个服务起不来。"""

    settings: Settings

    @cached_property
    def vault(self) -> FileVault:
        return FileVault(self.settings.mybmo_data_dir, self.settings.master_key_bytes())

    @cached_property
    def registry(self) -> AccountRegistry:
        return AccountRegistry(self.vault)

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

    def transport_for(self, alias: str | None = None) -> BaiduTransport:
        return BaiduTransport(_AliasCredentials(self.credentials, alias), client=self.client)


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
