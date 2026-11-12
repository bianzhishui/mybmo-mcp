"""谁能用哪个账户。

对应 [ADR 0004](../../../docs/decisions/0004-multi-tenant-accounts.md) 的 D1。三条规则：

1. **默认拒绝**。没有授权就没有访问。多用户模式下授权表缺失是硬错误，不是"人人可用"——
   这是这类系统最容易写反、也最致命的一处。
2. **档位有序**。``read < write < admin``，高档位包含低档位。
3. **别名不是凭据**。知道别人的 alias 不等于能用它，必须落在自己的授权列表里。

当前是文件式实现（``data/tenancy.json``），只覆盖单租户：账户还在文件凭据库里，
没有 ``tenant_id`` 维度。多租户需要把账户表落到 PostgreSQL，
届时把 :class:`TenancyStore` 换成仓储实现，接口不变。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from mybmo_mcp.baidu.errors import BaiduError

PERMISSION_READ = "read"
PERMISSION_WRITE = "write"
PERMISSION_ADMIN = "admin"

#: 从小到大的权限档位
PERMISSIONS: tuple[str, ...] = (PERMISSION_READ, PERMISSION_WRITE, PERMISSION_ADMIN)
_RANK = {PERMISSION_READ: 1, PERMISSION_WRITE: 2, PERMISSION_ADMIN: 3}

MODE_LOCAL = "local"
MODE_OIDC = "oidc"

#: 授权表格式版本。
#: - v1：只有 ``grants``（服务级共享账户，谁能用哪个别名）——历史格式，仍受理。
#: - v2：增加 ``accounts`` 段，账户**归某个用户私有**；owner 隐式拥有 admin，
#:   别人要用必须显式 grant（只能 read/write）。
#: **组织/应用维度不在这里表达**：本服务只对应 Casdoor 的 pandabase/mybmo-mcp 应用，
#: 跨组织在鉴权层（aud 白名单 + owner 校验）就被挡住了，授权表不该重复表达它。
TENANCY_VERSION = 2
SUPPORTED_TENANCY_VERSIONS: tuple[int, ...] = (1, 2)


class TenancyError(BaiduError):
    """授权表本身有问题：缺失、损坏、字段非法。

    这不是"无权"，是配置错误——两者的修复动作完全不同，不能混成一个错误。
    """


class AccessDenied(BaiduError):
    """当前身份对该账户没有足够权限。

    继承 :class:`BaiduError` 是为了让工具边界能把它翻译成结构化失败；
    它与百度接口无关，``code`` 恒为 ``None``。
    """

    def __init__(
        self,
        message: str,
        *,
        sub: str,
        alias: str | None,
        required: str,
        granted: str | None = None,
    ) -> None:
        super().__init__(message)
        self.sub = sub
        self.alias = alias
        self.required = required
        self.granted = granted

    @property
    def hint(self) -> str:
        if self.granted is None:
            return (
                "当前身份没有该账户的任何授权。授权关系记在 data/tenancy.json 的 grants 里，"
                "或用 list_accounts 看当前身份能访问哪些账户。"
            )
        return (
            f"当前只有 {self.granted} 权限，本次操作需要 {self.required}。"
            "调用方不应自行降级权限，应由账户管理员调整授权。"
        )


def satisfies(granted: str | None, required: str) -> bool:
    """``granted`` 是否满足 ``required``。未知档位一律不满足。"""
    if granted not in _RANK or required not in _RANK:
        return False
    return _RANK[granted] >= _RANK[required]


def required_permission(mutating: str) -> str:
    """按能力的 ``mutating`` 三态决定所需档位。

    ``unknown`` 按写处理——判不准时偏向更严的一侧，这是唯一安全的默认。
    """
    return PERMISSION_READ if mutating == "false" else PERMISSION_WRITE


@dataclass(frozen=True, slots=True)
class Principal:
    """一次调用的身份。``sub`` 是身份提供方的稳定 ID，不是邮箱、不是用户名。"""

    sub: str
    tenant_id: str = ""
    display_name: str = ""
    source: str = MODE_OIDC

    #: 身份所属组织（Casdoor 令牌里的 ``owner``）。v2 授权表按组织隔离，
    #: 跨组织的 grant 一律不可见——built-in 与 pandabase 是两个世界。
    org: str = ""
    #: 签发本次令牌的应用（``azp`` / ``client_id``）。grant 里写了 ``app`` 时按它再收窄一层。
    app: str = ""

    #: 本地单用户模式下的隐式全权身份。只允许 :meth:`local_admin` 构造出来。
    unrestricted: bool = False

    @classmethod
    def local_admin(cls) -> Principal:
        """stdio 单用户模式：本机进程即本人，对全部账户有 admin。

        这不是"跳过鉴权"，而是"身份就是本机用户"——stdio 没有别的调用者。
        HTTP 模式下绝不能回退到这个身份，:meth:`Runtime.current_principal` 会拦住。
        """
        return cls(
            sub="local",
            tenant_id="local",
            display_name="本地用户",
            source=MODE_LOCAL,
            unrestricted=True,
        )

    def to_public(self) -> dict[str, Any]:
        return {
            "sub": self.sub,
            "org": self.org,
            "app": self.app,
            "tenant_id": self.tenant_id,
            "display_name": self.display_name,
            "source": self.source,
            "unrestricted": self.unrestricted,
        }


@dataclass(frozen=True, slots=True)
class Grant:
    """一条分享：把这个账户（别人的私有账户）以某档位授权给某个用户。

    只允许 ``read`` / ``write``——``admin`` 是 owner 的天然权利，写进 grant 会让人
    误以为"给别人也能给管理权"，所以解析时直接拒绝。
    """

    sub: str
    alias: str
    permission: str


@dataclass(frozen=True, slots=True)
class Account:
    """一个已绑定百度账户的归属记录。

    ``alias`` 必须与凭据库里的文件名一致；``owner`` 是绑定它的人（Casdoor 的 sub），
    owner 隐式拥有 admin 档。**没有登记在这里的 alias 一律不可见**（fail closed）。
    """

    alias: str
    owner: str
    user_name: str = ""
    note: str = ""


#: 文件后端（``data/tenancy.json``）已随 ADR 0009 移除；授权表只剩 PostgreSQL 实现。
#: 这里保留纯逻辑与模型（Principal / Account / Grant / 判权比较），以及下面这个协议：
#: 调用方和测试替身都按它编程，不必依赖具体实现。


@runtime_checkable
class TenancyStore(Protocol):
    """授权表的最小契约。PG 实现与测试替身都满足它。"""

    @property
    def mode(self) -> str: ...

    def resolve_principal(
        self, sub: str, *, org: str = "", app: str = "", fallback_name: str = ""
    ) -> Principal: ...

    def permission_for(self, principal: Principal, alias: str) -> str | None: ...

    def visible(self, principal: Principal, aliases: list[str]) -> list[str]: ...

    def require(self, principal: Principal, alias: str, permission: str) -> None: ...

    def register_account(
        self, alias: str, *, owner: str, user_name: str = "", note: str = ""
    ) -> None: ...

    def unregister_account(self, alias: str) -> bool: ...

    def share(self, alias: str, *, sub: str, permission: str) -> None: ...

    def revoke(self, alias: str, *, sub: str) -> bool: ...

    def shares_of(self, alias: str) -> dict[str, str]: ...

    def owner_of(self, alias: str) -> str | None: ...

    def accounts(self) -> list[Account]: ...

    def describe(self, principal: Principal) -> dict[str, Any]: ...

    def stats(self) -> dict[str, int]: ...
