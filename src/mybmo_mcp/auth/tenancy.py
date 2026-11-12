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

import json
from dataclasses import dataclass, replace
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


class TenancyStore:
    """授权表。进程内首次使用时加载，测试可 ``reload()``。

    文件格式（``data/tenancy.json``）::

        {
          "version": 2,
          "users": [
            {"sub": "u_zhang", "org": "pandabase", "tenant_id": "t_acme", "display_name": "张三"}
          ],
          "grants": [
            {"org": "pandabase", "sub": "u_zhang", "alias": "axd", "permission": "write"}
          ]
        }

    **v2 的三条隔离规则**（v1 文件按"单租户、不校验组织"处理，只为兼容旧部署）：

    1. ``grants[].org`` 必填，且必须等于令牌的 ``owner``（组织）。**不跨组织关联权限**——
       built-in 组织的授权不会给 pandabase 的用户用，反之亦然。
    2. ``grants[].app`` 可选；写了就要求令牌的 ``azp``（client_id）一致，
       用于同一组织内多应用之间再隔离一层。
    3. ``users`` 不是必需的：只写 grants 也能工作。写上是为了让 ``sub`` 有可读的名字。
       含 ``role`` 之类的字段会被忽略——**所有权限只来自 grants**，没有隐含的管理员。
    """

    def __init__(self, path: str | Path, *, mode: str = MODE_LOCAL) -> None:
        self._path = Path(path)
        self._mode = mode
        self._loaded = False
        self._unrestricted = False
        self._legacy_shared = False
        self._users: dict[str, Principal] = {}
        self._accounts: dict[str, Account] = {}
        self._grants: list[Grant] = []
        #: 上次加载时的 (mtime_ns, size)。文件被改过就重新加载——
        #: 否则"改了 tenancy.json 不重启不生效"会变成一个很难查的坑（实测踩过）。
        self._stamp: tuple[int, int] | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def mode(self) -> str:
        return self._mode

    def reload(self) -> None:
        self._loaded = False
        self._ensure_loaded()

    # ---------- 身份 ----------

    def resolve_principal(
        self, sub: str, *, org: str = "", app: str = "", fallback_name: str = ""
    ) -> Principal:
        """把 JWT 的 ``sub`` 映射成身份。

        **未登记的身份不自动创建**。用户表缺失时也允许通过——但那时它拿不到任何
        grants，等于什么都做不了。这样"新用户登录成功但看不到账户"是可解释的状态，
        而不是一个悄悄拥有权限的账户。

        授权表整体缺失时也不抛错：身份解析发生在鉴权之后、判权之前。
        在这里抛错会让"没建 tenancy.json"表现为 500，而实际应该是
        "登录成功，但没有可用账户"。真正的拒绝发生在 :meth:`require`。
        """
        if not self._path.exists():
            return Principal(sub=sub, org=org, app=app, tenant_id="", source=MODE_OIDC)
        self._ensure_loaded()
        user = self._users.get(sub)
        if user is None:
            return Principal(
                sub=sub, org=org, app=app, display_name=fallback_name, tenant_id="",
                source=MODE_OIDC,
            )
        if not user.display_name and fallback_name:
            # 授权表没填名字时，用令牌里的 name 兜底（审计需要"谁调的"）
            user = replace(user, display_name=fallback_name)
        return replace(user, org=org, app=app)

    # ---------- 查询 ----------

    def permission_for(self, principal: Principal, alias: str) -> str | None:
        """返回该身份对该 alias 的档位；没有授权返回 ``None``。

        多条 grant 命中时取**最高档**：同一个人可能既有一条组织级、又有一条应用级授权。
        组织不匹配的 grant 直接忽略（v2）。
        """
        self._ensure_loaded()
        if principal.unrestricted:
            return PERMISSION_ADMIN
        account = self._accounts.get(alias)
        if account is not None and account.owner == principal.sub:
            # 自己能绑就能管：owner 不必写 grant
            return PERMISSION_ADMIN
        best: str | None = None
        for grant in self._grants:
            if grant.sub != principal.sub or grant.alias != alias:
                continue
            if best is None or _RANK[grant.permission] > _RANK[best]:
                best = grant.permission
        return best

    def visible(self, principal: Principal, aliases: list[str]) -> list[str]:
        """从候选 alias 里筛出该身份能访问的，保持原顺序。"""
        self._ensure_loaded()
        if principal.unrestricted:
            return list(aliases)
        return [alias for alias in aliases if self.permission_for(principal, alias) is not None]

    def register_account(
        self, alias: str, *, owner: str, user_name: str = "", note: str = ""
    ) -> None:
        """登记账户归属（绑定成功后调用）。

        写文件是**读-改-写 + 原子替换**：这个文件是权限的唯一事实源，
        写坏或写一半等于全站失权。非 v2 文件会被补齐成 v2。
        """
        self._ensure_loaded()
        alias = validate_alias(alias)
        if not owner:
            raise TenancyError("登记账户缺 owner：账户必须归某个 sub 所有")
        data = self._read_raw()
        data["version"] = TENANCY_VERSION
        accounts = [
            dict(item)
            for item in (data.get("accounts") or [])  # type: ignore[union-attr]
            if isinstance(item, dict) and str(item.get("alias") or "").strip().lower() != alias
        ]
        accounts.append(
            {"alias": alias, "owner": owner, "user_name": user_name, "note": note}
        )
        data["accounts"] = accounts
        self._write_raw(data)
        self.reload()

    def share(self, alias: str, *, sub: str, permission: str) -> None:
        """把账户以 read/write 分享给别人（admin 只属于 owner）。"""
        self._ensure_loaded()
        permission = permission.strip().lower()
        if permission not in (PERMISSION_READ, PERMISSION_WRITE):
            raise TenancyError("分享只能给 read 或 write；admin 是 owner 的天然权利")
        alias = validate_alias(alias)
        data = self._read_raw()
        data["version"] = TENANCY_VERSION
        grants = [
            dict(item)
            for item in (data.get("grants") or [])  # type: ignore[union-attr]
            if isinstance(item, dict)
            and not (
                str(item.get("sub") or "") == sub
                and str(item.get("alias") or "").strip().lower() == alias
            )
        ]
        grants.append({"sub": sub, "alias": alias, "permission": permission})
        data["grants"] = grants
        self._write_raw(data)
        self.reload()

    def revoke(self, alias: str, *, sub: str) -> bool:
        """撤销某人对某账户的分享。返回是否真的删掉了一条。"""
        self._ensure_loaded()
        alias = validate_alias(alias)
        data = self._read_raw()
        grants = [
            dict(item)
            for item in (data.get("grants") or [])  # type: ignore[union-attr]
            if isinstance(item, dict)
            and not (str(item.get("sub") or "") == sub
                     and str(item.get("alias") or "").strip().lower() == alias)
        ]
        removed = len(grants) != len(data.get("grants") or [])
        if removed:
            data["version"] = TENANCY_VERSION
            data["grants"] = grants
            self._write_raw(data)
            self.reload()
        return removed

    def shares_of(self, alias: str) -> dict[str, str]:
        """某账户被分享给了谁：``{sub: permission}``。"""
        self._ensure_loaded()
        return {g.sub: g.permission for g in self._grants if g.alias == alias}

    def _read_raw(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"version": TENANCY_VERSION, "users": [], "accounts": [], "grants": []}
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TenancyError(f"授权表顶层必须是对象：{self._path}")
        return raw

    def _write_raw(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:  # 某些文件系统不支持，权限由部署保证
            pass
        tmp.replace(self._path)

    def stats(self) -> dict[str, int]:
        """给探活用的人数统计。**只返回计数，不返回任何 sub / 别名**。

        用来观察"什么时候真的出现了第二个用户"——那正是多租户落库的触发条件。
        """
        self._ensure_loaded()
        tenants = {p.tenant_id for p in self._users.values() if p.tenant_id}
        return {
            "tenants": len(tenants),
            "users": len(self._users),
            "accounts": len(self._accounts),
            "grants": len(self._grants),
        }

    def owner_of(self, alias: str) -> str | None:
        """账户归属的 sub；未登记返回 ``None``（v1 老文件没有归属概念）。"""
        self._ensure_loaded()
        account = self._accounts.get(alias)
        return account.owner if account else None

    def accounts(self) -> list[Account]:
        self._ensure_loaded()
        return list(self._accounts.values())

    def require(self, principal: Principal, alias: str, permission: str) -> None:
        """不足则抛 :class:`AccessDenied`。这是唯一应该被调用的判定入口。"""
        granted = self.permission_for(principal, alias)
        if granted is None:
            account = self._accounts.get(alias)
            reason = (
                f"该账户属于别人（owner={account.owner}），需要 owner 在 tenancy.json 里给你授权"
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

    def describe(self, principal: Principal) -> dict[str, Any]:
        """给 ``list_accounts`` 用的身份摘要，不含任何令牌。"""
        self._ensure_loaded()
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
            "account_model": "shared" if self._legacy_shared else "owned",
        }

    # ---------- 加载 ----------

    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            st = self._path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _ensure_loaded(self) -> None:
        stamp = self._file_stamp()
        if self._loaded and stamp == self._stamp:
            return
        if self._loaded:
            # 文件在运行期被改过：丢弃缓存重新解析
            self._loaded = False

        if not self._path.exists():
            if self._mode != MODE_LOCAL:
                raise TenancyError(
                    f"多用户模式（mode={self._mode}）下缺少授权表 {self._path}。"
                    "不创建它就拒绝一切账户调用是有意为之——回退成「人人可用」的代价远大于停机。"
                )
            self._unrestricted = True
            self._stamp = stamp
            self._loaded = True
            return

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise TenancyError(f"授权表不是合法 JSON：{self._path}（{exc}）") from exc
        if not isinstance(raw, dict):
            raise TenancyError(f"授权表顶层必须是对象：{self._path}")

        version = raw.get("version", TENANCY_VERSION)
        if version not in SUPPORTED_TENANCY_VERSIONS:
            raise TenancyError(
                f"授权表版本 {version} 不受支持"
                f"（支持 {' / '.join(str(v) for v in SUPPORTED_TENANCY_VERSIONS)}）：{self._path}"
            )
        self._legacy_shared = int(version) < 2

        self._users = self._parse_users(raw.get("users") or [])
        self._accounts = self._parse_accounts(raw.get("accounts") or [])
        self._grants = self._parse_grants(raw.get("grants") or [])
        self._unrestricted = False
        self._stamp = stamp
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

    def _parse_accounts(self, items: Any) -> dict[str, Account]:
        if not isinstance(items, list):
            raise TenancyError(f"授权表的 accounts 必须是数组：{self._path}")
        accounts: dict[str, Account] = {}
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise TenancyError(f"授权表 accounts[{index}] 必须是对象：{self._path}")
            alias = str(item.get("alias") or "").strip().lower()
            owner = str(item.get("owner") or "").strip()
            if not alias:
                raise TenancyError(f"授权表 accounts[{index}] 缺少 alias：{self._path}")
            try:
                validate_alias(alias)
            except CredentialError as exc:
                raise TenancyError(
                    f"授权表 accounts[{index}] 的 alias 不合法（{alias!r}）：{exc}"
                ) from exc
            if not owner:
                raise TenancyError(
                    f"授权表 accounts[{index}]（{alias}）缺少 owner："
                    f"账户必须归某个 sub 所有，否则等于人人可用（{self._path}）"
                )
            if alias in accounts:
                raise TenancyError(f"授权表 accounts 里 alias 重复：{alias}（{self._path}）")
            accounts[alias] = Account(
                alias=alias,
                owner=owner,
                user_name=str(item.get("user_name") or ""),
                note=str(item.get("note") or ""),
            )
        return accounts

    def _parse_grants(self, items: Any) -> list[Grant]:
        if not isinstance(items, list):
            raise TenancyError(f"授权表的 grants 必须是数组：{self._path}")
        grants: list[Grant] = []
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
            if self._legacy_shared is False and permission == PERMISSION_ADMIN:
                raise TenancyError(
                    f"授权表 grants[{index}] 给 {sub} 授予了 {alias} 的 admin："
                    "admin 是账户 owner 的天然权利，不能通过 grant 转授；"
                    f"要共享就授 read 或 write（{self._path}）"
                )
            for existing in grants:
                if existing.sub == sub and existing.alias == alias:
                    raise TenancyError(
                        f"授权表里 {sub} 对 {alias} 有重复授权，无法判断以哪条为准：{self._path}"
                    )
            grants.append(Grant(sub=sub, alias=alias, permission=permission))
        return grants
