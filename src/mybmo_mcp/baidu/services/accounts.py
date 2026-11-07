"""账户与资金（只读）。

对应接口：

- ``AccountService/getAccountInfo`` 账户基本信息
- ``BalanceService/getBalanceInfo`` 余额与预算（需额外的账户管理权限）

两个接口的权限域是分开的。权限没开通时余额会返回 ``89406``——这时不该让整个
快照失败，而是把「哪部分没拿到、为什么」如实写进结果，让用户去补权限。
"""

from __future__ import annotations

from typing import Any

from mybmo_mcp.baidu.errors import BaiduApiError
from mybmo_mcp.baidu.transport import BaiduTransport

#: 只挑这些字段做摘要。接口没给的字段**不补默认值**，
#: 避免把「没有这个字段」渲染成「余额是 0」。
SUMMARY_FIELDS = (
    "userId",
    "userName",
    "isActive",
    "balance",
    "budget",
    "cost",
    "regDate",
    "region",
)


def _first_object(data: Any, *, service: str) -> dict[str, Any]:
    if isinstance(data, list):
        candidate = data[0] if data else None
    else:
        candidate = data
    if candidate is None:
        return {}
    if not isinstance(candidate, dict):
        raise BaiduApiError(
            f"{service} 返回结构与契约不符：期望对象或对象列表",
            service=service,
            outcome="unknown",
        )
    return candidate


async def snapshot(transport: BaiduTransport, *, include_balance: bool = True) -> dict[str, Any]:
    """账户快照：基本信息 + 尽力取余额。

    余额取不到时返回 ``balance: null`` 与 ``balance_error``，不吞掉错误码。
    """
    data = await transport.call("AccountService", "getAccountInfo", {})
    account = _first_object(data, service="AccountService/getAccountInfo")

    result: dict[str, Any] = {
        "summary": {key: account[key] for key in SUMMARY_FIELDS if key in account},
        "extra": {key: value for key, value in account.items() if key not in SUMMARY_FIELDS},
    }
    if not account:
        result["note"] = "接口未返回账户信息，可能是权限域未开通或账户异常"

    if include_balance:
        try:
            result["balance"] = await transport.call("BalanceService", "getBalanceInfo", {})
        except BaiduApiError as exc:
            result["balance"] = None
            result["balance_error"] = {
                "code": exc.code,
                "message": str(exc),
                "hint": exc.hint,
            }
    return result
