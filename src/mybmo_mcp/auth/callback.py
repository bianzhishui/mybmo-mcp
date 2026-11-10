"""百度授权回调：把浏览器带回来的 ``authCode + state`` 换成凭据。

抽成纯函数是为了能在没有 HTTP 层的情况下测透——路由处理器只做参数搬运，
判定与错误文案都在这里。

回调地址是**公网可达的普通 URL，任何人都会访问到**（百度跳转过来的是浏览器，
带不了 Bearer 令牌），所以这里守三条：

1. **不信任 state**：它只用来定位 alias；缺失或不匹配时由
   :class:`~mybmo_mcp.auth.binding.BindingManager` 降级成 ``acct_<userId>`` +
   ``unconfirmed``，把判断权交回给人，而不是在回调里"猜一个账户"。
2. **对外只有一句话**：不区分"没有这个 state"与"state 被别人用了"，避免探测。
3. **不回显任何令牌片段**：只回 alias 与账户中文名，错误信息也只来自
   :class:`~mybmo_mcp.baidu.errors.CredentialError`——那个类本身已保证不含
   authCode / secretKey / accessToken。

回调参数名**尚未实证**（见 ``docs/baidu-behavior.md`` 的「state 是否原样回传」），
所以 ``authCode`` / ``code`` 与 ``userId`` / ``uid`` 都接受；全部缺失时明确报错，
让日志里留下"百度改过参数名"的证据，而不是静默当成未绑定。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mybmo_mcp.auth.binding import BindingManager
from mybmo_mcp.auth.models import STATUS_ACTIVE
from mybmo_mcp.baidu.errors import BaiduError, CredentialError

#: 候选参数名，按优先级排。第一项是百度营销侧既有实现里见到的写法。
AUTH_CODE_KEYS: tuple[str, ...] = ("authCode", "code", "auth_code")
USER_ID_KEYS: tuple[str, ...] = ("userId", "uid", "user_id")
ERROR_KEYS: tuple[str, ...] = ("error", "errorCode")
ERROR_DESC_KEYS: tuple[str, ...] = ("error_description", "errorMessage", "errorMsg")

#: 授权码是一次性短字符串，超过这个长度只能是伪造或上游异常。
MAX_AUTH_CODE_LEN = 2048


@dataclass(frozen=True, slots=True)
class CallbackOutcome:
    """回调处理结果。``status_code`` 直接给 HTTP 层用。"""

    ok: bool
    status_code: int
    message: str
    hints: tuple[str, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)


def _first(params: Mapping[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = params.get(key)
        if value:
            return str(value).strip()
    return ""


def _parse_user_id(raw: str) -> int:
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


async def complete_binding(
    manager: BindingManager,
    params: Mapping[str, str],
) -> CallbackOutcome:
    """用回调参数完成一次绑定。不抛异常，全部以 :class:`CallbackOutcome` 表达。"""
    error = _first(params, ERROR_KEYS)
    if error:
        description = _first(params, ERROR_DESC_KEYS) or "用户取消或拒绝了授权"
        return CallbackOutcome(
            ok=False,
            status_code=400,
            message=f"授权未完成：{description}",
            data={"error": error},
        )

    auth_code = _first(params, AUTH_CODE_KEYS)
    if not auth_code:
        return CallbackOutcome(
            ok=False,
            status_code=400,
            message="回调缺少授权码（authCode），没有可换取的凭据",
            hints=(
                "确认百度应用里登记的授权回调地址与本服务配置的 "
                "MYBMO_BAIDU_CALLBACK_URL 一致",
                "百度可能改过回调参数名，把这次请求的查询参数记进 docs/baidu-behavior.md",
            ),
        )
    if len(auth_code) > MAX_AUTH_CODE_LEN:
        return CallbackOutcome(ok=False, status_code=400, message="授权码长度异常，拒绝处理")

    user_id = _parse_user_id(_first(params, USER_ID_KEYS))
    state = _first(params, ("state",))

    try:
        credential = await manager.complete(auth_code=auth_code, state=state, user_id=user_id)
    except CredentialError as exc:
        # CredentialError 的文案由 auth/oauth.py 保证不含授权码与任何令牌
        return CallbackOutcome(
            ok=False,
            status_code=400,
            message=f"换取令牌失败：{exc}",
            hints=("授权码只能用一次，重新执行 mybmo-mcp auth bind 生成新链接",),
        )
    except BaiduError as exc:
        return CallbackOutcome(
            ok=False,
            status_code=502,
            message=f"百度授权服务返回异常：{exc}",
            hints=("这是上游问题，先重试一次 auth bind；连续失败再查 docs/baidu-behavior.md",),
        )

    hints: list[str] = []
    if credential.status != STATUS_ACTIVE:
        hints.append(
            "state 缺失或不匹配，账户按 userId 命名并标记为 unconfirmed；"
            "确认 userName 是本账户中文名后再改为 active"
        )
    if not user_id:
        hints.append("回调里没有 userId，换令牌请求里填的是 0；若百度报错属已知未验证项")

    return CallbackOutcome(
        ok=True,
        status_code=200,
        message="绑定成功",
        hints=tuple(hints),
        data={
            "alias": credential.alias,
            "user_name": credential.user_name,
            "status": credential.status,
            "expires_at": credential.expires_at,
        },
    )
