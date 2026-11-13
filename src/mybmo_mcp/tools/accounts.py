"""账户类工具：本项目**自身**的能力，不是百度接口的封装。

百度接口一律通过能力网关（``search_capabilities`` / ``get_capability_schema`` /
``execute_capability``）调用，这里只保留三件事：**看账户、看令牌、绑账户**。

- ``list_accounts`` / ``auth_status``：不联网，只读本地加密凭据库
- ``bind_account``：发起一次百度 OAuth 绑定，**用户自己在浏览器里完成授权**，
  回调后服务自动落库，并把这个账户的 owner 记成**发起人自己**——这是"用户自助"
  的关键一步：不给运维打电话也能有自己的百度账户。

前两个不联网；``bind_account`` 只生成链接，也不替用户点授权。
三个工具都经过授权层——**看不到的账户不会出现在列表里**。这不是过滤展示，
而是越权防线的一部分：模型只能从返回的别名里选。
"""

from __future__ import annotations

import time
from typing import Any

from mcp.server import MCPServer

from mybmo_mcp.auth.models import STATUS_REAUTH_REQUIRED, validate_alias
from mybmo_mcp.baidu.errors import CredentialError
from mybmo_mcp.tools._result import guarded
from mybmo_mcp.tools.runtime import get_runtime


async def list_accounts() -> dict[str, Any]:
    """列出**当前身份**可访问的百度账户（别名、中文名、是否可用、是否默认）。

    返回里的 identity 说明你是谁、在这个服务里的 sub 与租户，以及你对每个账户的权限档位
    （read / write / admin）。找不到某个账户时的第一站：先确认它是否在 accounts 里，
    不在就是没有授权，换别名没用。

    调用任何账户级能力前先看这个列表：账户只能从返回的别名里选，
    不能自己编一个，也不能拿别的账户的令牌去试。
    """

    async def _run() -> dict[str, Any]:
        runtime = get_runtime()
        principal = runtime.current_principal()
        ctx = runtime.context(principal)
        entries = ctx.access.entries(principal)
        return {
            "identity": ctx.tenancy.describe(principal),
            "count": len(entries),
            "accounts": [_view(ctx, principal, entry) for entry in entries],
        }

    return await guarded(_run)


def _view(ctx: Any, principal: Any, entry: Any) -> dict[str, Any]:
    """列表行 + 归属信息。

    ``is_owner`` 很关键：自己绑的账户与别人分享的是两种心智模型——
    前者可以改、可以分享，后者只能按档位用。
    """
    view = entry.to_dict()
    owner = ctx.tenancy.owner_of(entry.alias)
    view["owner"] = owner
    view["is_owner"] = owner == principal.sub
    view["my_permission"] = ctx.tenancy.permission_for(principal, entry.alias)
    return view


async def auth_status(account: str | None = None) -> dict[str, Any]:
    """查看令牌状态：是否可用、还有多久过期、是否需要重新授权。

    排查「突然调不通」时先看这里。``needs_reauth`` 为真说明 refreshToken 已失效，
    重新刷新没有意义，必须重新走一次授权绑定。

    ``account`` 留空看所有已授权账户；传别名时必须是 list_accounts 返回过的。
    """

    async def _run() -> list[dict[str, Any]]:
        runtime = get_runtime()
        principal = runtime.current_principal()
        ctx = runtime.context(principal)
        entries = ctx.access.entries(principal)
        if account:
            # 走授权层而不是直接查凭据库：无权的 alias 与不存在的 alias 给同一条错误，
            # 否则这个工具就成了账户名探测器。
            entries = [ctx.access.resolve(principal, account)]
        now = time.time()
        return [
            {
                **entry.to_dict(),
                "needs_reauth": entry.status == STATUS_REAUTH_REQUIRED,
                "expires_in_seconds": (
                    max(0, int(entry.expires_at - now)) if entry.expires_at else None
                ),
            }
            for entry in entries
        ]

    return await guarded(_run)


async def bind_account(alias: str, app_id: str = "") -> dict[str, Any]:
    """发起一次百度账户绑定，返回一个授权链接——**由你本人在浏览器里完成授权**。

    ``alias`` 是给这个账户起的别名（小写字母开头，可含数字与下划线），之后所有调用都用它。
    链接有效期 15 分钟、state 一次性；在浏览器里登录百度并同意授权后，服务会自动换取令牌、
    加密落库，并把这个账户的 owner 记成**你**（owner 自带 admin 档）。

    什么时候用它：``list_accounts`` 返回 0 个账户时。**不要自己编别名去用**，
    先绑一个自己的，或让已有账户的 owner 分享给你。
    """

    async def _run() -> dict[str, Any]:
        runtime = get_runtime()
        principal = runtime.current_principal()
        ctx = runtime.context(principal)
        name = validate_alias(alias)
        owner = ctx.tenancy.owner_of(name)
        if owner and owner != principal.sub:
            raise CredentialError(
                f"账户别名 {name} 已属于别人（owner={owner}）——换一个别名，"
                "或请 owner 用分享把该账户授给你"
            )
        existing = set(ctx.store.scan()[0])
        if name in existing and owner is None:
            raise CredentialError(
                f"凭据库里已有 {name}，但授权表没有它的归属记录；"
                f"先让账户负责人补 accounts[{{alias: {name}, owner: ...}}]，"
                "否则绑了也没人能看见"
            )
        manager = runtime.binding
        resolved_app = app_id.strip()
        if not resolved_app:
            known = [app.app_id for app in ctx.app_secrets.list()]
            if len(known) == 1:
                resolved_app = known[0]
            else:
                listed = "、".join(known) if known else "（无）"
                raise CredentialError(
                    f"必须指定 app_id。已登记的应用：{listed}。"
                    "密钥需要在服务器上预登记：mybmo-mcp app secret set <appId>"
                )
        url, state = manager.authorize_url(
            subject=principal.sub,
            tenant=principal.tenant_id,
            alias=name,
            app_id=resolved_app,
        )
        return {
            "authorize_url": url,
            "alias": state.alias,
            "app_id": state.app_id,
            "owner": state.subject,
            "expires_at": state.expires_at,
            "expires_in_seconds": int(state.expires_at - time.time()),
            "how_to": (
                "在浏览器打开 authorize_url，用要绑定的百度账户登录并同意授权；"
                "百度会跳回本服务的回调地址，页面显示「绑定成功」后这个账户就能用了"
            ),
        }

    return await guarded(_run)


async def share_account(
    alias: str, sub: str, permission: str = "read"
) -> dict[str, Any]:
    """把自己绑定的百度账户分享给别人（**只有 owner 能调**）。

    ``permission`` 只能是 ``read`` 或 ``write`` —— ``admin`` 是 owner 的天然权利，不能转授。
    对方要用之前必须先用 ``list_accounts`` 确认能看到这个账户；分享后立刻生效，不用重启。
    """

    async def _run() -> dict[str, Any]:
        runtime = get_runtime()
        principal = runtime.current_principal()
        ctx = runtime.context(principal)
        name = validate_alias(alias)
        target = (sub or "").strip()
        if not target:
            raise CredentialError("缺少 sub：用对方的 list_accounts → identity.sub 取值")
        level = (permission or "read").strip().lower()
        if level not in ("read", "write"):
            raise CredentialError("分享只能给 read 或 write；admin 是 owner 的天然权利，不能转授")
        owner = ctx.tenancy.owner_of(name)
        if owner != principal.sub:
            raise CredentialError(
                f"只有账户 owner 能分享：{name} 的 owner 是 "
                f"{owner or '未登记'}，不是 {principal.sub}"
            )
        ctx.tenancy.share(name, sub=target, permission=level)
        return {
            "alias": name,
            "shared_with": target,
            "permission": level,
            "shares": ctx.tenancy.shares_of(name),
        }

    return await guarded(_run)


async def revoke_share(alias: str, sub: str) -> dict[str, Any]:
    """撤销分享（**只有 owner 能调**）。"""

    async def _run() -> dict[str, Any]:
        runtime = get_runtime()
        principal = runtime.current_principal()
        ctx = runtime.context(principal)
        name = validate_alias(alias)
        owner = ctx.tenancy.owner_of(name)
        if owner != principal.sub:
            raise CredentialError(
                f"只有账户 owner 能撤销分享：{name} 的 owner 是 {owner or '未登记'}"
            )
        removed = ctx.tenancy.revoke(name, sub=(sub or "").strip())
        return {"alias": name, "revoked": removed, "shares": ctx.tenancy.shares_of(name)}

    return await guarded(_run)


async def list_shares(alias: str) -> dict[str, Any]:
    """看某个账户分享给了谁（owner 或 admin 可见）。"""

    async def _run() -> dict[str, Any]:
        runtime = get_runtime()
        principal = runtime.current_principal()
        ctx = runtime.context(principal)
        name = validate_alias(alias)
        owner = ctx.tenancy.owner_of(name)
        if owner != principal.sub:
            raise CredentialError(
                f"只有账户 owner 能查看分享列表：{name} 的 owner 是 {owner or '未登记'}"
            )
        return {"alias": name, "owner": owner, "shares": ctx.tenancy.shares_of(name)}

    return await guarded(_run)


def register(mcp: MCPServer) -> None:
    mcp.tool()(list_accounts)
    mcp.tool()(auth_status)
    mcp.tool()(bind_account)
    mcp.tool()(share_account)
    mcp.tool()(revoke_share)
    mcp.tool()(list_shares)
