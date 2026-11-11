"""百度账户绑定：一次性 state + 回调换令牌。

流程：

1. :meth:`BindingManager.authorize_url` 生成**一次性 nonce**（不是
   ``md5(appId_uid)`` 这类可推导值），绑定 ``subject + tenant + alias`` 与 15 分钟 TTL
2. 用户浏览器完成授权，百度回调带 ``authCode + state``
3. :meth:`BindingManager.complete` 消费 state → 换令牌 → 取 ``masterName`` → 落库

百度是否原样回传自定义 ``state`` **尚未实证**。因此当 state 缺失或不匹配时，
不失败也不直接信任：按 ``userId`` 命名账户（``acct_<userId>``）并标记
``unconfirmed``，把判断权交回给人。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from mybmo_mcp.auth.models import (
    STATUS_ACTIVE,
    STATUS_UNCONFIRMED,
    Credential,
    validate_alias,
)
from mybmo_mcp.auth.oauth import PLATFORM_ID, BaiduOAuth, build_authorize_url
from mybmo_mcp.auth.tenancy import TenancyStore
from mybmo_mcp.auth.vault import FileVault
from mybmo_mcp.baidu.errors import CredentialError

STATE_TTL = 900.0
STATE_DIRNAME = "binding_state"
_STATE_MODE = 0o600
_DIR_MODE = 0o700
#: nonce 直接做文件名，只允许 URL 安全字符，长度下限挡住弱随机。
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


@dataclass(frozen=True, slots=True)
class BindingState:
    nonce: str
    subject: str
    tenant: str
    alias: str
    issued_at: float
    expires_at: float


class StateStore:
    """一次性 state 的持久化。进程重启不丢，取用即删除。"""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        ttl: float = STATE_TTL,
        now=time.time,
    ) -> None:
        self.root = Path(root) / STATE_DIRNAME
        self._ttl = ttl
        self._now = now
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, _DIR_MODE)

    def issue(self, *, subject: str, tenant: str, alias: str) -> BindingState:
        alias = validate_alias(alias)
        if not subject:
            raise CredentialError("缺少 subject，无法签发绑定 state")
        now = self._now()
        state = BindingState(
            nonce=secrets.token_urlsafe(32),
            subject=subject,
            tenant=tenant,
            alias=alias,
            issued_at=now,
            expires_at=now + self._ttl,
        )
        path = self._path(state.nonce)
        payload = {
            "nonce": state.nonce,
            "subject": state.subject,
            "tenant": state.tenant,
            "alias": state.alias,
            "issued_at": state.issued_at,
            "expires_at": state.expires_at,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.chmod(path, _STATE_MODE)
        return state

    def consume(self, nonce: str) -> BindingState | None:
        """取用并销毁。返回 ``None`` 表示不存在、已用过或已过期。"""
        if not nonce or not _NONCE_RE.match(nonce):
            return None
        path = self._path(nonce)
        if not path.is_file():
            return None
        # 先读后删：删除即作废，哪怕后面解析失败也不能重放。
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        path.unlink()
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        if float(data.get("expires_at") or 0) <= self._now():
            return None
        return BindingState(
            nonce=str(data.get("nonce", nonce)),
            subject=str(data.get("subject", "")),
            tenant=str(data.get("tenant", "")),
            alias=str(data.get("alias", "")),
            issued_at=float(data.get("issued_at") or 0.0),
            expires_at=float(data.get("expires_at") or 0.0),
        )

    def _path(self, nonce: str) -> Path:
        return self.root / f"{nonce}.json"


class BindingManager:
    """把「签发授权链接」与「回调落库」串起来。"""

    def __init__(
        self,
        oauth: BaiduOAuth,
        vault: FileVault,
        states: StateStore,
        *,
        app_id: str,
        secret_key: str,
        redirect_uri: str,
        platform_id: str = PLATFORM_ID,
        tenancy: TenancyStore | None = None,
    ) -> None:
        self._oauth = oauth
        self._vault = vault
        self._states = states
        self._app_id = app_id
        self._secret_key = secret_key
        self._redirect_uri = redirect_uri
        self._platform_id = platform_id
        #: 有它就在绑定成功后自动登记账户归属（账户私有，没 owner 等于谁都看不见）
        self._tenancy = tenancy

    def authorize_url(self, *, subject: str, tenant: str, alias: str) -> tuple[str, BindingState]:
        state = self._states.issue(subject=subject, tenant=tenant, alias=alias)
        url = build_authorize_url(
            self._app_id,
            callback=self._redirect_uri,
            state=state.nonce,
            platform_id=self._platform_id,
        )
        return url, state

    async def complete(
        self, *, auth_code: str, state: str, user_id: int
    ) -> tuple[Credential, BindingState | None]:
        """处理回调。换令牌失败时不回显 authCode。

        返回 ``(凭据, 命中的 state)``——调用方要用 state 里的 ``subject`` 把账户归属
        写进授权表（账户是私有的，没有 owner 等于谁都看不见）。
        """
        resolved_state = self._states.consume(state) if state else None

        bundle = await self._oauth.exchange(auth_code, user_id)
        info = await self._oauth.user_info(
            bundle.access_token, user_id=bundle.user_id or user_id, open_id=bundle.open_id
        )

        user_name = str(info.get("masterName") or info.get("userName") or "").strip()
        if not user_name:
            raise CredentialError(
                "getUserInfo 未返回 masterName，无法确定账户中文名；缺少它后续每次调用都会报 81020"
            )

        if resolved_state is not None:
            alias = resolved_state.alias
            status = STATUS_ACTIVE
            note = ""
        else:
            alias = validate_alias(f"acct_{bundle.user_id or user_id}")
            status = STATUS_UNCONFIRMED
            note = "state 缺失或不匹配，按 userId 命名，待人工确认"

        credential = Credential(
            alias=alias,
            user_name=user_name,
            user_id=bundle.user_id or user_id,
            app_id=self._app_id,
            secret_key=self._secret_key,
            access_token=bundle.access_token,
            refresh_token=bundle.refresh_token,
            open_id=bundle.open_id,
            expires_at=bundle.expires_at,
            refresh_expires_at=bundle.refresh_expires_at,
            scope=bundle.scope,
            status=status,
            note=note,
        )
        self._vault.save(credential)
        if resolved_state is not None and self._tenancy is not None:
            # 归属登记失败不能吞掉：凭据已经落库，但账户会因此对所有人不可见，
            # 必须在回调页面上让操作者看到（由调用方把状态回显出去）。
            self._tenancy.register_account(
                credential.alias, owner=resolved_state.subject, user_name=credential.user_name
            )
        return credential, resolved_state
