"""账户类工具：本项目**自身**的能力，不是百度接口的封装。

百度接口一律通过能力网关（``search_capabilities`` / ``get_capability_schema`` /
``execute_capability``）调用，这里只保留"用哪个账户"和"令牌还行不行"这两件事。

它们不联网：读的是本地加密凭据库，因此没有 AppID、没有网络时也能用。

两个工具都经过授权层——**看不到的账户不会出现在列表里**。这不是过滤展示，
而是越权防线的一部分：模型只能从返回的别名里选。
"""

from __future__ import annotations

import time
from typing import Any

from mcp.server import MCPServer

from mybmo_mcp.auth.models import STATUS_REAUTH_REQUIRED
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
        entries = runtime.access.entries(principal)
        return {
            "identity": runtime.tenancy.describe(principal),
            "count": len(entries),
            "accounts": [entry.to_dict() for entry in entries],
        }

    return await guarded(_run)


async def auth_status(account: str | None = None) -> list[dict[str, Any]]:
    """查看令牌状态：是否可用、还有多久过期、是否需要重新授权。

    排查「突然调不通」时先看这里。``needs_reauth`` 为真说明 refreshToken 已失效，
    重新刷新没有意义，必须重新走一次授权绑定。

    ``account`` 留空看所有已授权账户；传别名时必须是 list_accounts 返回过的。
    """

    async def _run() -> list[dict[str, Any]]:
        runtime = get_runtime()
        principal = runtime.current_principal()
        entries = runtime.access.entries(principal)
        if account:
            # 走授权层而不是直接查凭据库：无权的 alias 与不存在的 alias 给同一条错误，
            # 否则这个工具就成了账户名探测器。
            entries = [runtime.access.resolve(principal, account)]
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


def register(mcp: MCPServer) -> None:
    mcp.tool()(list_accounts)
    mcp.tool()(auth_status)
