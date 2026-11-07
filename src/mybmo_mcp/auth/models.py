"""凭据模型。

一个百度推广账户 = 一份凭据 = 一个 alias。``user_name`` 是账户**中文名**，
不是数字 userId——百度接口要求请求头里填中文名，填错报 ``81020``。

时间统一用 epoch 秒（float）。百度 OAuth 返回的是 ``expiresIn`` 相对秒数，
直接换算比在任意时区里折腾 ``datetime`` 更不容易出错；落库到 PostgreSQL 时
再由数据层转 ``timestamptz``。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from typing import Any

from mybmo_mcp.baidu.errors import CredentialError

#: alias 同时是文件名，规则必须挡住路径分隔符与大写字母。
ALIAS_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"
_ALIAS_RE = re.compile(ALIAS_PATTERN)

#: 提前 5 分钟视为过期，避免请求在路上失效
EXPIRY_SKEW = 300.0

STATUS_ACTIVE = "active"
STATUS_REAUTH_REQUIRED = "reauth_required"
STATUS_UNCONFIRMED = "unconfirmed"


def validate_alias(alias: str) -> str:
    """校验并返回 alias。不合法直接抛错，不做静默纠偏。"""
    if not isinstance(alias, str) or not _ALIAS_RE.match(alias):
        raise CredentialError(
            f"账户别名不合法：{alias!r}。要求匹配 {ALIAS_PATTERN}，"
            "即小写字母或数字开头，只含小写字母、数字、下划线、连字符，最长 32 字符"
        )
    return alias


@dataclass(frozen=True, slots=True)
class Credential:
    """一个推广账户的授权凭据。不可变：刷新令牌时返回新实例。"""

    alias: str
    user_name: str = ""
    user_id: int = 0
    app_id: str = ""
    secret_key: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    open_id: str = ""
    expires_at: float = 0.0
    refresh_expires_at: float = 0.0
    scope: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_ACTIVE
    note: str = ""

    def __post_init__(self) -> None:
        validate_alias(self.alias)

    # ---------- 有效期 ----------

    def access_expired(self, now: float | None = None) -> bool:
        if not self.expires_at:
            return False
        return (now if now is not None else time.time()) >= self.expires_at - EXPIRY_SKEW

    def refresh_expired(self, now: float | None = None) -> bool:
        if not self.refresh_expires_at:
            return False
        return (now if now is not None else time.time()) >= self.refresh_expires_at

    def is_usable(self, now: float | None = None) -> bool:
        """能否立刻发请求：有未过期令牌，或能靠 refreshToken 续上。"""
        if self.status == STATUS_REAUTH_REQUIRED:
            return False
        if self.access_token and not self.access_expired(now):
            return True
        return bool(self.refresh_token) and not self.refresh_expired(now)

    def needs_refresh(self, now: float | None = None) -> bool:
        return not self.access_token or self.access_expired(now)

    # ---------- 变更 ----------

    def with_tokens(
        self,
        *,
        access_token: str,
        refresh_token: str | None = None,
        expires_at: float | None = None,
        refresh_expires_at: float | None = None,
        open_id: str | None = None,
        user_id: int | None = None,
        scope: dict[str, Any] | None = None,
    ) -> Credential:
        """返回带新令牌的副本。``refresh_token`` 为 None 表示沿用旧值。"""
        return replace(
            self,
            access_token=access_token,
            refresh_token=self.refresh_token if refresh_token is None else refresh_token,
            expires_at=self.expires_at if expires_at is None else expires_at,
            refresh_expires_at=(
                self.refresh_expires_at if refresh_expires_at is None else refresh_expires_at
            ),
            open_id=self.open_id if open_id is None else open_id,
            user_id=self.user_id if user_id is None else user_id,
            scope=self.scope if scope is None else scope,
            status=STATUS_ACTIVE,
        )

    def mark_reauth_required(self, reason: str = "") -> Credential:
        return replace(
            self,
            status=STATUS_REAUTH_REQUIRED,
            note=reason or "刷新失败，需要重新授权",
        )

    # ---------- 序列化 ----------

    def to_storage(self) -> dict[str, Any]:
        """完整字段，用于加密落盘。**不要**直接返回给模型或日志。"""
        return {
            "alias": self.alias,
            "user_name": self.user_name,
            "user_id": self.user_id,
            "app_id": self.app_id,
            "secret_key": self.secret_key,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "open_id": self.open_id,
            "expires_at": self.expires_at,
            "refresh_expires_at": self.refresh_expires_at,
            "scope": self.scope,
            "status": self.status,
            "note": self.note,
        }

    @classmethod
    def from_storage(cls, data: dict[str, Any]) -> Credential:
        """从落盘结构还原。忽略未知字段，容忍缺字段（旧版本文件）。"""
        return cls(
            alias=str(data["alias"]),
            user_name=str(data.get("user_name", "")),
            user_id=int(data.get("user_id") or 0),
            app_id=str(data.get("app_id", "")),
            secret_key=str(data.get("secret_key", "")),
            access_token=str(data.get("access_token", "")),
            refresh_token=str(data.get("refresh_token", "")),
            open_id=str(data.get("open_id", "")),
            expires_at=float(data.get("expires_at") or 0.0),
            refresh_expires_at=float(data.get("refresh_expires_at") or 0.0),
            scope=dict(data.get("scope") or {}),
            status=str(data.get("status", STATUS_ACTIVE)),
            note=str(data.get("note", "")),
        )

    def to_public(self, now: float | None = None) -> dict[str, Any]:
        """对外展示用。只给别名、账户名与可用性，**不含任何令牌**。"""
        return {
            "alias": self.alias,
            "user_name": self.user_name,
            "usable": self.is_usable(now),
            "status": self.status,
            "expires_at": self.expires_at,
            "refresh_expires_at": self.refresh_expires_at,
            "note": self.note,
        }
