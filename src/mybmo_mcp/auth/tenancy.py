"""谁能用哪个账户。

对应 [ADR 0004](../../../docs/decisions/0004-multi-tenant-accounts.md) 的 D1。三条规则：

1. **默认拒绝**。没有授权就没有访问。多用户模式下授权表缺失是硬错误，不是"人人可用"——
   这是这类系统最容易写反、也最致命的一处。
2. **档位有序**。``read < write < admin``，高档位包含低档位。
3. **别名不是凭据**。知道别人的 alias 不等于能用它，必须落在自己的授权列表里。

当前是文件式实现（``data/tenancy.json``），只覆盖单租户：账户还在文件凭据库里，
没有 ``tenant_id`` 维度。多租户要等 P4 把账户表落到 PostgreSQL（tasks 5.10），
届时把 :class:`TenancyStore` 换成仓储实现，接口不变。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mybmo_mcp.auth.models import validate_alias
from mybmo_mcp.baidu.errors import BaiduError, CredentialError

PERMISSION_READ = "read"
PERMISSION_WRITE = "write"
PERMISSION_ADMIN = "admin"

#: 从小到大的权限档位
PERMISSIONS: tuple[str, ...] = (PERMISSION_READ, PERMISSION_WRITE, PERMISSION_ADMIN)
_RANK = {PERMISSION_READ: 1, PERMISSION_WRITE: 2, PERMISSION_ADMIN: 3}

MODE_LOCAL = "local"
MODE_OIDC = "oidc"

TENANCY_VERSION = 1


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
            "tenant_id": self.tenant_id,
            "display_name": self.display_name,
            "source": self.source,
            "unrestricted": self.unrestricted,
        }


@dataclass(frozen=True, slots=True)
class Grant:
    sub: str
    alias: str
    permission: str


class TenancyStore:
    """授权表。进程内首次使用时加载，测试可 ``reload()``。

    文件格式（``data/tenancy.json``）::

        {
          "version": 1,
          "users": [
            {"sub": "u_zhang", "tenant_id": "t_acme", "display_name": "张三"}
          ],
          "grants": [
            {"sub": "u_zhang", "alias": "axd", "permission": "write"}
          ]
        }

    ``users`` 不是必需的：只写 grants 也能工作。写上是为了让 ``sub`` 有可读的名字。
    含 ``role`` 之类的字段会被忽略——**当前所有权限只来自 grants**，没有隐含的管理员。
    """

    def __init__(self, path: str | Path, *, mode: str = MODE_LOCAL) -> None:
        self._path = Path(path)
        self._mode = mode
        self._loaded = False
        self._unrestricted = False
        self._users: dict[str, Principal] = {}
        self._grants: dict[str, dict[str, str]] = {}

    @property
    def path(self) -> Path:
        return self._path

    @property
    def mode(self) -> str:
        return self._mode

    def reload(self) -> None:
        self._loaded = False
        self._ensure_loaded()

    # ---------- 查询 ----------

    def permission_for(self, principal: Principal, alias: str) -> str | None:
        """返回该身份对该 alias 的档位；没有授权返回 ``None``。"""
        self._ensure_loaded()
        if principal.unrestricted:
            return PERMISSION_ADMIN
        return self._grants.get(principal.sub, {}).get(alias)

    def visible(self, principal: Principal, aliases: list[str]) -> list[str]:
        """从候选 alias 里筛出该身份能访问的，保持原顺序。"""
        self._ensure_loaded()
        if principal.unrestricted:
            return list(aliases)
        owned = self._grants.get(principal.sub, {})
        return [alias for alias in aliases if alias in owned]

    def require(self, principal: Principal, alias: str, permission: str) -> None:
        """不足则抛 :class:`AccessDenied`。这是唯一应该被调用的判定入口。"""
        granted = self.permission_for(principal, alias)
        if granted is None:
            raise AccessDenied(
                f"当前身份（{principal.sub}）没有账户 {alias} 的授权",
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
        """给 ``list_accounts`` 用的身份摘要，不含任何令牌。"""
        self._ensure_loaded()
        grants = {} if principal.unrestricted else dict(self._grants.get(principal.sub, {}))
        return {
            **principal.to_public(),
            "grants": grants,
            "mode": self._mode,
        }

    # ---------- 加载 ----------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return

        if not self._path.exists():
            if self._mode != MODE_LOCAL:
                raise TenancyError(
                    f"多用户模式（mode={self._mode}）下缺少授权表 {self._path}。"
                    "不创建它就拒绝一切账户调用是有意为之——回退成「人人可用」的代价远大于停机。"
                )
            self._unrestricted = True
            self._loaded = True
            return

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise TenancyError(f"授权表不是合法 JSON：{self._path}（{exc}）") from exc
        if not isinstance(raw, dict):
            raise TenancyError(f"授权表顶层必须是对象：{self._path}")

        version = raw.get("version", TENANCY_VERSION)
        if version != TENANCY_VERSION:
            raise TenancyError(
                f"授权表版本 {version} 不受支持（期望 {TENANCY_VERSION}）：{self._path}"
            )

        self._users = self._parse_users(raw.get("users") or [])
        self._grants = self._parse_grants(raw.get("grants") or [])
        self._unrestricted = False
        self._loaded = True

    def _parse_users(self, items: Any) -> dict[str, Principal]:
        if not isinstance(items, list):
            raise TenancyError(f"授权表的 users 必须是数组：{self._path}")
        users: dict[str, Principal] = {}
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise TenancyError(f"授权表 users[{index}] 必须是对象：{self._path}")
            sub = str(item.get("sub") or "").strip()
            if not sub:
                raise TenancyError(f"授权表 users[{index}] 缺少 sub：{self._path}")
            if sub in users:
                raise TenancyError(f"授权表 users 里 sub 重复：{sub}（{self._path}）")
            users[sub] = Principal(
                sub=sub,
                tenant_id=str(item.get("tenant_id") or ""),
                display_name=str(item.get("display_name") or ""),
                source=MODE_OIDC,
            )
        return users

    def _parse_grants(self, items: Any) -> dict[str, dict[str, str]]:
        if not isinstance(items, list):
            raise TenancyError(f"授权表的 grants 必须是数组：{self._path}")
        grants: dict[str, dict[str, str]] = {}
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise TenancyError(f"授权表 grants[{index}] 必须是对象：{self._path}")
            sub = str(item.get("sub") or "").strip()
            alias = str(item.get("alias") or "").strip().lower()
            permission = str(item.get("permission") or PERMISSION_READ).strip().lower()
            if not sub:
                raise TenancyError(f"授权表 grants[{index}] 缺少 sub：{self._path}")
            try:
                validate_alias(alias)
            except CredentialError as exc:
                raise TenancyError(
                    f"授权表 grants[{index}] 的 alias 不合法（{sub} → {alias!r}）：{exc}"
                ) from exc
            if permission not in _RANK:
                raise TenancyError(
                    f"授权表 grants[{index}] 的 permission 是 {permission!r}，"
                    f"只能是 {' / '.join(PERMISSIONS)}：{self._path}"
                )
            owned = grants.setdefault(sub, {})
            if alias in owned:
                raise TenancyError(
                    f"授权表里 {sub} 对 {alias} 有重复授权，无法判断以哪条为准：{self._path}"
                )
            owned[alias] = permission
        return grants
