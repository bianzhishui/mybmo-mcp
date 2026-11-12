"""百度账户绑定：一次性 state + 回调换令牌。

流程（ADR 0006 D3/D4/D6）：

1. :meth:`BindingManager.authorize_url` 按 ``app_id`` 取应用密钥（由
   ``mybmo-mcp app secret set`` 预登记），**预写一条 ``pending`` 占位凭据**，
   再签发一次性 nonce，绑定 ``subject + tenant + alias + app_id`` 与 15 分钟 TTL
2. 用户浏览器完成授权，百度回调带 ``authCode + state + appId + userId``
3. :meth:`BindingManager.complete` 消费 state → 按 alias 取回占位凭据里的应用密钥
   → 换令牌 → 取 ``masterName`` → 查 1:1 唯一性 → 落库并登记 owner

**state 缺失不再瞎猜**：实测百度确实会原样回传 ``state``，但也自带 ``appId``。
所以 state 缺失时按回调的 ``appId`` 兜底（应用已知），按 ``userId`` 命名成
``acct_<userId>`` 并标 ``unconfirmed``；连 ``appId`` 都没有才失败——那时既不知道
用哪把密钥，也不知道是哪个应用，硬猜只会把凭据存到错误的应用名下。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mybmo_mcp.auth.app_secrets import AppSecretStore
from mybmo_mcp.auth.models import (
    STATUS_ACTIVE,
    STATUS_PENDING,
    STATUS_UNCONFIRMED,
    Credential,
    validate_alias,
)
from mybmo_mcp.auth.oauth import PLATFORM_ID, BaiduOAuth, build_authorize_url
from mybmo_mcp.auth.store import CredentialStore
from mybmo_mcp.auth.tenancy import TenancyStore
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
    #: 这次绑定用的是哪个应用。回调要靠它找回应用密钥（ADR 0006 D4）
    app_id: str = ""


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

    def issue(
        self, *, subject: str, tenant: str, alias: str, app_id: str = ""
    ) -> BindingState:
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
            app_id=app_id,
        )
        path = self._path(state.nonce)
        payload = {
            "nonce": state.nonce,
            "subject": state.subject,
            "tenant": state.tenant,
            "alias": state.alias,
            "app_id": state.app_id,
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
            app_id=str(data.get("app_id", "")),
        )

    def _path(self, nonce: str) -> Path:
        return self.root / f"{nonce}.json"


class BindingManager:
    """把「签发授权链接」与「回调落库」串起来。"""

    def __init__(
        self,
        oauth: Any,
        store: CredentialStore,
        states: StateStore,
        *,
        redirect_uri: str,
        app_secrets: AppSecretStore | None = None,
        platform_id: str = PLATFORM_ID,
        tenancy: TenancyStore | None = None,
        app_id: str = "",
        secret_key: str = "",
    ) -> None:
        self._store = store
        self._states = states
        self._redirect_uri = redirect_uri
        self._app_secrets = app_secrets
        self._platform_id = platform_id
        #: 有它就在绑定成功后自动登记账户归属（账户私有，没 owner 等于谁都看不见）
        self._tenancy = tenancy
        #: 兼容/测试用：直接给一个 oauth 实例（生产不开，按应用现造）
        self._oauth = oauth
        #: 兼容：遗留的"默认应用"，只在应用登记表里查不到时使用
        self._app_id = app_id
        self._secret_key = secret_key

    # ---------- 签发 ----------

    def authorize_url(
        self, *, subject: str, tenant: str, alias: str, app_id: str = ""
    ) -> tuple[str, BindingState]:
        alias = validate_alias(alias)
        resolved_app = (app_id or self._app_id).strip()
        secret, scope = self._app_credentials(resolved_app)

        # 预写占位凭据：state 里只放 alias 与 app_id，密钥从这条记录取回，
        # 不往 state 文件里塞明文密钥。
        self._store.save(
            Credential(
                alias=alias,
                app_id=resolved_app,
                secret_key=secret,
                scope={},
                status=STATUS_PENDING,
                note="预登记，等待授权回调",
            )
        )

        state = self._states.issue(
            subject=subject, tenant=tenant, alias=alias, app_id=resolved_app
        )
        url = build_authorize_url(
            resolved_app,
            callback=self._redirect_uri,
            state=state.nonce,
            scope=scope,
            platform_id=self._platform_id,
        )
        return url, state

    # ---------- 回调 ----------

    async def complete(
        self, *, auth_code: str, state: str, user_id: int, app_id: str = ""
    ) -> tuple[Credential, BindingState | None]:
        """处理回调。换令牌失败时不回显 authCode。

        返回 ``(凭据, 命中的 state)``——调用方要用 state 里的 ``subject`` 把账户归属
        写进授权表（账户是私有的，没有 owner 等于谁都看不见）。
        """
        resolved_state = self._states.consume(state) if state else None

        if resolved_state is not None:
            alias = resolved_state.alias
            resolved_app = resolved_state.app_id or app_id or self._app_id
            status = STATUS_ACTIVE
            note = ""
        else:
            # state 缺失：应用已知（回调自带 appId）才能继续，否则只能失败——
            # 既不知道用哪把密钥，也不知道是哪个应用。
            resolved_app = app_id or self._app_id
            if not resolved_app:
                raise CredentialError(
                    "回调既没有 state 也没有 appId，无法确定用哪个应用的密钥；"
                    "请重新执行一次绑定"
                )
            if not user_id:
                raise CredentialError(
                    "回调没有 state 也没有 userId，无法定位账户；请重新执行一次绑定"
                )
            alias = validate_alias(f"acct_{user_id}")
            status = STATUS_UNCONFIRMED
            note = "state 缺失或不匹配，按 userId 命名，待人工确认"

        secret = self._pending_secret(alias, resolved_app)
        oauth, owned = self._oauth_for(resolved_app, secret)
        try:
            bundle = await oauth.exchange(auth_code, user_id)
            info = await oauth.user_info(
                bundle.access_token, user_id=bundle.user_id or user_id, open_id=bundle.open_id
            )
        finally:
            if owned:
                await oauth.aclose()

        user_name = str(info.get("masterName") or info.get("userName") or "").strip()
        if not user_name:
            raise CredentialError(
                "getUserInfo 未返回 masterName，无法确定账户中文名；缺少它后续每次调用都会报 81020"
            )

        resolved_user_id = bundle.user_id or user_id
        self._assert_no_conflict(
            alias=alias, app_id=resolved_app, baidu_user_id=resolved_user_id
        )

        credential = Credential(
            alias=alias,
            user_name=user_name,
            user_id=resolved_user_id,
            app_id=resolved_app,
            secret_key=secret,
            access_token=bundle.access_token,
            refresh_token=bundle.refresh_token,
            open_id=bundle.open_id,
            expires_at=bundle.expires_at,
            refresh_expires_at=bundle.refresh_expires_at,
            scope=bundle.scope,
            status=status,
            note=note,
        )
        self._store.save(credential)
        if resolved_state is not None and self._tenancy is not None:
            # 归属登记失败不能吞掉：凭据已经落库，但账户会因此对所有人不可见，
            # 必须在回调页面上让操作者看到（由调用方把状态回显出去）。
            self._tenancy.register_account(
                credential.alias, owner=resolved_state.subject, user_name=credential.user_name
            )
        return credential, resolved_state

    # ---------- 内部 ----------

    def _app_credentials(self, app_id: str) -> tuple[str, str]:
        """返回 ``(secret_key, scope)``。查不到就报错并告诉用户怎么登记。"""
        if not app_id:
            raise CredentialError(
                "缺少应用 ID（appId）。绑定必须指定应用，见 list_apps 或 mybmo-mcp app secret list"
            )
        if self._app_secrets is not None:
            app = self._app_secrets.get(app_id)
            if app is not None and app.secret_key:
                return app.secret_key, app.scope
        if app_id == self._app_id and self._secret_key:
            return self._secret_key, ""
        raise CredentialError(
            f"应用 {app_id} 未登记密钥。先在服务器上执行："
            f"mybmo-mcp app secret set {app_id}（密钥为隐藏输入，不会进入对话）"
        )

    def _pending_secret(self, alias: str, app_id: str) -> str:
        """优先用占位凭据里存的那把密钥；没有就回落到应用登记表。"""
        try:
            credential = self._store.load(alias)
        except CredentialError:
            credential = None
        if (
            credential is not None
            and credential.app_id == app_id
            and credential.secret_key
        ):
            return credential.secret_key
        secret, _ = self._app_credentials(app_id)
        return secret

    def _oauth_for(self, app_id: str, secret_key: str) -> tuple[Any, bool]:
        """返回 ``(oauth, 是否由这里创建)``。创建的要负责关闭。"""
        if self._oauth is not None:
            return self._oauth, False
        return BaiduOAuth(app_id, secret_key), True

    def _assert_no_conflict(self, *, alias: str, app_id: str, baidu_user_id: int) -> None:
        """ADR 0006 D6：一个百度账户只绑一个应用、一个应用只服务一个账户。

        PG 后端的唯一索引会兜住，但文件后端没有约束，所以这里做一次显式检查，
        让两个后端的行为一致；冲突时**拒绝而不是覆盖**，避免一组授权无声失效。
        """
        try:
            aliases = self._store.aliases()
        except CredentialError:
            aliases = []
        for other in aliases:
            if other == alias:
                continue
            try:
                credential = self._store.load(other)
            except CredentialError:
                continue
            if credential.status == STATUS_PENDING:
                continue
            if baidu_user_id and credential.user_id == baidu_user_id:
                raise CredentialError(
                    f"该百度账户已绑定应用 {credential.app_id or '(未知)'}…（别名 {other}）。"
                    "本项目规定一个百度账户只能绑定一个应用，如需换用其他应用，"
                    f"请先执行 mybmo-mcp auth unbind {other}"
                )
            if app_id and credential.app_id == app_id:
                raise CredentialError(
                    f"应用 {app_id} 已绑定账户 {other}。一个应用只能服务一个账户，"
                    f"如需换绑请先执行 mybmo-mcp auth unbind {other}"
                )
