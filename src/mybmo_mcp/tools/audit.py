"""审计查询工具：``query_audit``。

**隔离规则（与授权层一致）**：

* 所有查询强制带 ``tenant_id``——跨租户永远查不到
* 普通用户只能看到 ``user_sub = 自己`` 的记录
* 账户 owner 额外能看**自己账户下**所有人的调用（排查"谁动了我的计划"必需）
* 显式传别人的 ``user`` 一律拒绝，不做"悄悄降级成自己"

后台实现的可见范围计算放在这一层，SQL 只接收已经算好的
``visible_subs`` / ``visible_aliases``，避免把权限逻辑散进查询语句里。
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from mybmo_mcp.audit.base import STATES
from mybmo_mcp.audit.pg import PgAuditSink
from mybmo_mcp.auth.tenancy import AccessDenied
from mybmo_mcp.tools._result import guarded
from mybmo_mcp.tools.runtime import get_runtime

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


async def query_audit(
    started_after: str | None = None,
    started_before: str | None = None,
    account: str | None = None,
    capability: str | None = None,
    state: str | None = None,
    http_status: int | None = None,
    error_code: int | None = None,
    call_id: str | None = None,
    user: str | None = None,
    params_match: dict[str, Any] | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    include_payload: bool = False,
) -> dict[str, Any]:
    """查调用审计（谁在什么时候调了百度哪个接口、结果如何）。

    默认只返回元数据；要看入参/出参原文需显式 ``include_payload=True``（单条可能很大）。
    ``started_after`` / ``started_before`` 用 ISO8601，如 ``2026-11-12T00:00:00+08:00``。
    """

    async def _run() -> dict[str, Any]:
        runtime = get_runtime()
        principal = runtime.current_principal()
        ctx = runtime.context(principal)

        sink = runtime.audit
        if not isinstance(sink, PgAuditSink):
            raise ValueError(
                "审计查询需要 MYBMO_AUDIT_BACKEND=pg；当前是文件后端。"
                "文件审计直接看 /data/audit/YYYY-MM-DD.jsonl"
            )

        if state is not None and state not in STATES:
            raise ValueError(f"state 只能是 {' / '.join(STATES)}")

        size = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
        start = max(0, int(offset or 0))

        # ---- 可见范围 ----
        unrestricted = principal.unrestricted
        owned: list[str] = []
        if not unrestricted:
            owned = [
                item.alias
                for item in ctx.tenancy.accounts()
                if item.owner == principal.sub
            ]

        if user and not unrestricted and user != principal.sub:
            raise AccessDenied(
                f"不能查别人的调用记录：当前身份是 {principal.sub}",
                sub=principal.sub,
                alias=user,
                required="read",
            )

        if account and not unrestricted:
            if account not in owned and ctx.tenancy.permission_for(principal, account) is None:
                raise AccessDenied(
                    f"当前身份（{principal.sub}）看不到账户 {account} 的审计",
                    sub=principal.sub,
                    alias=account,
                    required="read",
                )

        total, rows = sink.query(
            tenant_id=principal.tenant_id or "",
            visible_subs=None if unrestricted else [principal.sub],
            visible_aliases=None if unrestricted else owned,
            started_after=started_after,
            started_before=started_before,
            account=account,
            capability=capability,
            state=state,
            http_status=http_status,
            error_code=error_code,
            call_id=call_id,
            user_sub=user or None,
            params_match=params_match,
            include_payload=include_payload,
            limit=size,
            offset=start,
        )

        return {
            "total": total,
            "limit": size,
            "offset": start,
            "next_offset": start + size if start + size < total else None,
            "scope": _describe(principal, account, unrestricted),
            "rows": [_jsonable(row) for row in rows],
        }

    return await guarded(_run)


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    """时间戳转 ISO8601，其余原样。"""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def _describe(principal: Any, account: str | None, unrestricted: bool) -> dict[str, Any]:
    return {
        "sub": principal.sub,
        "tenant_id": principal.tenant_id,
        "scope": "all" if unrestricted else "self+owned",
        "account": account or "",
    }


def register(mcp: MCPServer) -> None:
    mcp.tool()(query_audit)
