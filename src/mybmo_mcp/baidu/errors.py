"""百度营销 API 的错误类型与错误码释义。

只做一件事：把百度返回的 ``code`` / ``message`` / ``position`` 翻译成人能看懂、
且能直接照做的修复建议。调用方不需要知道错误码细节。

错误信息一律经过 :func:`redact` 脱敏，不允许出现 accessToken、refreshToken、
secretKey、authCode 等凭据片段——这些串一旦进日志就等于泄漏。
"""

from __future__ import annotations

import re
from typing import Any

#: code -> (接口原始信息, 修复建议)
#:
#: 这些提示来自真实账户的踩坑记录，不是文档抄写。改之前先确认为什么改。
ERROR_HINTS: dict[int, tuple[str, str]] = {
    8002: (
        "The Json message you provided is invalid",
        "鉴权信息放错了位置。userName / accessToken / action 必须在请求体 JSON 的 "
        "header 字段里，不能放 HTTP 头。",
    ),
    81012: (
        "Username is not set in request header",
        "header 里的键名必须是首字母大写的 userName，写成 username 会报此错。",
    ),
    81020: (
        "The username information in the request is incorrect",
        "userName 与 accessToken 不属于同一个账户，多账户场景下的典型错误。"
        "按 alias 取凭据，不要手动拼凑不同账户的令牌与账户名。",
    ),
    84261: (
        "The token is invalid in encrypt",
        "accessToken 不能放在 token 字段。token 属于旧的加密通道，OAuth 令牌必须放 accessToken。",
    ),
    87091: (
        "request url is invalid",
        "服务名或方法名大小写错误。KRService 的 KR 两个字母都必须大写，"
        "方法是 getKRByQuery 而不是 getKrByQuery。",
    ),
    89406: (
        "The access token you provided is invalidate",
        "字面意思是令牌失效，但同一令牌能调通别的接口时，真实原因是该接口所属权限未开通。"
        "先用只读接口验证令牌本身，再确认应用是否申请了对应能力域。",
    ),
    901701: (
        "Seed Word is null",
        "body.query 为空。关键词规划师必须传种子词。",
    ),
    99912: (
        "JSON invalid",
        "参数类型不对。例如 getKRByQuery 的 query 是字符串而不是数组。",
    ),
}

#: 需要脱敏的键名。注意不能把 ``code`` 纳入——错误码本身就是正文。
_SENSITIVE_RE = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|secret[_-]?key|client[_-]?secret|auth[_-]?code)"
    r"\b(\s*[:=]\s*)([\"']?)([^\"'&,\s}]+)"
)


def redact(text: str) -> str:
    """把文本里的凭据片段替换成 ``***``。幂等，可重复调用。"""
    return _SENSITIVE_RE.sub(r"\1\2\3***", text)


class BaiduError(RuntimeError):
    """本项目所有百度相关异常的基类。"""


class BaiduApiError(BaiduError):
    """百度返回 ``header.status != 0``，或网络层失败。

    ``outcome`` 区分两种失败，决定了调用方能不能重试：

    - ``failed``：确定失败（业务错误码、4xx），重试没有意义
    - ``unknown``：结果未知（超时、5xx、响应不是 JSON），写操作必须先查后写
    """

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        position: str | None = None,
        service: str | None = None,
        method: str | None = None,
        outcome: str = "failed",
    ) -> None:
        super().__init__(redact(message))
        self.code = code
        self.position = position
        self.service = service
        self.method = method
        self.outcome = outcome

    @property
    def hint(self) -> str | None:
        entry = ERROR_HINTS.get(self.code) if self.code is not None else None
        return entry[1] if entry else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "code": self.code,
            "position": self.position,
            "service": self.service,
            "method": self.method,
            "hint": self.hint,
            "outcome": self.outcome,
        }


class CredentialError(BaiduError):
    """凭据缺失、已过期且无法刷新，或刷新被拒绝。"""


class WriteNotPermitted(BaiduError):
    """写操作被本地策略拦住。

    这不是网络或权限报错，而是主动拒绝执行——避免在无权限的情况下反复调用写接口，
    或让调用方误以为写成功。
    """


class ApprovalRequired(BaiduError):
    """写操作缺少审批，或审批凭据与当前报文不符。

    写操作不可逆，必须走「预览 → 用户确认 → 凭预览里的 approval_id 执行」。
    缺少 approval_id 说明没预览过，不匹配说明预览之后报文被改过，两种情况都必须重来。
    """


class DryRunRequested(Exception):
    """dry-run 模式下的短路信号。

    用异常而不是返回特殊字典，是为了不污染 services 层的返回类型——
    业务函数始终可以假定自己拿到的是真实 data 或正常异常。

    这里不继承 :class:`BaiduError`：它不是错误，是正常的控制流。
    """

    def __init__(
        self, url: str, payload: dict[str, Any], *, approval: dict[str, Any] | None = None
    ):
        super().__init__("dry-run")
        self.url = url
        self.payload = payload
        self.approval = approval

    def to_dict(self) -> dict[str, Any]:
        header = self.payload.get("header", {})
        safe = {
            "header": {
                "userName": header.get("userName"),
                "accessToken": "***",
                "action": header.get("action"),
            },
            "body": self.payload.get("body"),
        }
        result: dict[str, Any] = {"dry_run": True, "url": self.url, "payload": safe}
        if self.approval:
            result["approval"] = self.approval
        return result
