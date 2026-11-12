"""账户注册表：把凭据库与环境变量里的临时凭据合并成一份列表。

模型永远只能从这里返回的列表里选账户，不能自己编一个 alias，也不能直接传 userId——
多账户串号是这类服务最容易出的事故，防线就设在这一层。

环境变量凭据（``BAIDU_*``）是**临时覆盖**，用于 CI 或一次性调试：
它出现在列表里，但不会被设为默认，也不会被写回磁盘。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mybmo_mcp.auth.models import STATUS_PENDING, Credential, validate_alias
from mybmo_mcp.auth.store import CredentialStore
from mybmo_mcp.baidu.errors import CredentialError

ENV_ALIAS = "env"
SOURCE_FILE = "file"
SOURCE_ENV = "environment"


@dataclass(frozen=True, slots=True)
class AccountEntry:
    """列表里的一行。不含任何令牌字段。"""

    alias: str
    user_name: str
    usable: bool
    is_default: bool
    status: str
    source: str
    expires_at: float
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "user_name": self.user_name,
            "usable": self.usable,
            "is_default": self.is_default,
            "status": self.status,
            "source": self.source,
            "expires_at": self.expires_at,
            "note": self.note,
        }


def credential_from_env(env: Mapping[str, str] | None = None) -> Credential | None:
    """从环境变量构造临时凭据。缺账户名或令牌时返回 ``None``。"""
    env = env if env is not None else os.environ
    user_name = (env.get("BAIDU_USER_NAME") or "").strip()
    access_token = (env.get("BAIDU_ACCESS_TOKEN") or "").strip()
    if not user_name or not access_token:
        return None

    alias = (env.get("BAIDU_ALIAS") or ENV_ALIAS).strip().lower()
    try:
        user_id = int(env.get("BAIDU_USER_ID") or 0)
    except ValueError as exc:
        raise CredentialError("BAIDU_USER_ID 必须是整数") from exc

    return Credential(
        alias=validate_alias(alias),
        user_name=user_name,
        user_id=user_id,
        app_id=(env.get("BAIDU_APP_ID") or "").strip(),
        secret_key=(env.get("BAIDU_SECRET_KEY") or "").strip(),
        access_token=access_token,
        refresh_token=(env.get("BAIDU_REFRESH_TOKEN") or "").strip(),
        open_id=(env.get("BAIDU_OPEN_ID") or "").strip(),
        expires_at=_float_env(env, "BAIDU_EXPIRES_AT"),
        refresh_expires_at=_float_env(env, "BAIDU_REFRESH_EXPIRES_AT"),
    )


def _float_env(env: Mapping[str, str], key: str) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError as exc:
        raise CredentialError(f"{key} 必须是 epoch 秒（数字）") from exc


class AccountRegistry:
    """凭据库 + 环境变量的统一入口。"""

    def __init__(self, store: CredentialStore, *, env: Mapping[str, str] | None = None) -> None:
        self._store = store
        self._env = env

    @property
    def store(self) -> CredentialStore:
        """凭据存储（文件或 PostgreSQL，见 ADR 0007）。"""
        return self._store

    # ---------- 列表 ----------

    def list_accounts(self) -> list[AccountEntry]:
        """列出可用账户。

        存在损坏文件时**整体报错并列出全部路径**，不静默跳过——
        悄悄少一个账户，用户只会以为「授权丢了」，然后重复绑定。
        """
        aliases, errors = self._store.scan()
        if errors:
            detail = "；".join(f"{path}（{reason}）" for path, reason in errors)
            raise CredentialError(
                f"有 {len(errors)} 份凭据无法读取，请修复或删除后再试：{detail}"
            )

        default_alias = self._store.default_alias()
        entries: list[AccountEntry] = []

        env_cred = credential_from_env(self._env)
        if env_cred is not None:
            entries.append(self._entry(env_cred, is_default=False, source=SOURCE_ENV))

        for alias in aliases:
            credential = self._store.load(alias)
            # 预登记但还没完成授权的占位凭据不能出现在可选项里（ADR 0006 D4）
            if credential.status == STATUS_PENDING:
                continue
            entries.append(
                self._entry(credential, is_default=alias == default_alias, source=SOURCE_FILE)
            )
        return entries

    # ---------- 取用 ----------

    def get(self, alias: str) -> Credential:
        env_cred = credential_from_env(self._env)
        if env_cred is not None and alias == env_cred.alias:
            return env_cred
        return self._store.load(alias)

    def resolve(self, alias: str | None = None) -> Credential:
        """按 alias 取；未指定时依次尝试默认账户、环境变量、唯一账户。"""
        if alias:
            return self.get(alias)

        default_alias = self._store.default_alias()
        if default_alias:
            return self.get(default_alias)

        env_cred = credential_from_env(self._env)
        if env_cred is not None:
            return env_cred

        aliases, errors = self._store.scan()
        if errors:
            raise CredentialError(f"凭据损坏：{errors[0][0]}（{errors[0][1]}）")
        usable = [
            alias for alias in aliases if self._store.load(alias).status != STATUS_PENDING
        ]
        if len(usable) == 1:
            return self._store.load(usable[0])
        aliases = usable
        if not aliases:
            raise CredentialError(
                "没有任何已绑定的百度账户。先完成一次授权绑定，"
                "或设置 BAIDU_USER_NAME / BAIDU_ACCESS_TOKEN 作为临时凭据"
            )
        raise CredentialError(
            f"存在 {len(aliases)} 个账户，必须显式指定 alias：{', '.join(aliases)}"
        )

    def default_alias(self) -> str | None:
        return self._store.default_alias()

    def is_environment(self, alias: str) -> bool:
        """该 alias 是否指向环境变量提供的临时凭据（不可落盘）。"""
        env_cred = credential_from_env(self._env)
        return env_cred is not None and alias == env_cred.alias

    def set_default(self, alias: str) -> None:
        self._store.set_default(alias)

    # ---------- 内部 ----------

    @staticmethod
    def _entry(credential: Credential, *, is_default: bool, source: str) -> AccountEntry:
        return AccountEntry(
            alias=credential.alias,
            user_name=credential.user_name,
            usable=credential.is_usable(),
            is_default=is_default,
            status=credential.status,
            source=source,
            expires_at=credential.expires_at,
            note=credential.note,
        )
