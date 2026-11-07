"""账户类工具。

薄在两点：不拼报文、不判断业务规则；所有语义都在
``mybmo_mcp.baidu.services.accounts`` 里。
"""

from __future__ import annotations

import time
from typing import Any

from mcp.server import MCPServer

from mybmo_mcp.auth.models import STATUS_REAUTH_REQUIRED
from mybmo_mcp.baidu.errors import CredentialError
from mybmo_mcp.baidu.services import accounts as accounts_service
from mybmo_mcp.tools._result import guarded
from mybmo_mcp.tools.runtime import get_runtime


async def list_accounts() -> dict[str, Any]:
    """列出当前身份可访问的百度账户（别名、中文名、是否可用、是否默认）。

    调用任何账户级工具前先看这个列表：账户只能从返回的别名里选，
    不能自己编一个，也不能拿别的账户的令牌去试。
    """

    async def _run() -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in get_runtime().registry.list_accounts()]

    return await guarded(_run)


async def account_snapshot(account: str | None = None) -> dict[str, Any]:
    """账户快照：余额、预算、状态、当日消耗。

    ``account`` 传别名（见 list_accounts），留空则用默认账户。
    余额需要额外的账户管理权限；没开通时余额字段为 null 并给出错误码，其余字段照常返回。
    """

    async def _run() -> dict[str, Any]:
        transport = get_runtime().transport_for(account)
        return await accounts_service.snapshot(transport)

    return await guarded(_run)


async def auth_status(account: str | None = None) -> dict[str, Any]:
    """查看令牌状态：是否可用、还有多久过期、是否需要重新授权。

    排查「突然调不通」时先看这里。``needs_reauth`` 为真说明 refreshToken 已失效，
    重新刷新没有意义，必须重新走一次授权绑定。
    """

    async def _run() -> list[dict[str, Any]]:
        runtime = get_runtime()
        entries = runtime.registry.list_accounts()
        if account:
            entries = [entry for entry in entries if entry.alias == account]
            if not entries:
                raise CredentialError(f"未知账户：{account}，先调用 list_accounts 确认别名")
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
    mcp.tool()(account_snapshot)
    mcp.tool()(auth_status)
