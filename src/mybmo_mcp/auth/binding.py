"""百度账户绑定：一次性 state + 回调换令牌。

流程（ADR 0006 D3/D4/D6）：

1. :meth:`BindingManager.authorize_url` 按 ``app_id`` 取应用密钥（由
   ``mybmo-mcp app secret set`` 预登记），**预写一条 ``pending`` 占位凭据**，
   再签发一次性 nonce，绑定 ``subject + tenant + alias + app_id`` 与 15 分钟 TTL
2. 用户浏览器完成授权，百度回调带 ``authCode + state + appId + userId``
3. :meth:`BindingManager.complete` 消费 state → 按 alias 取回占位凭据里的应用密钥
   → 换令牌 → 取 ``masterName`` → 查 1:1 唯一性 → 落库并登记 owner

**state 是租户的唯一来源**（ADR 0008 D5）：回调那一跳来自浏览器，没有令牌可依据。
state 缺失/过期/重放一律失败——猜一个租户等于把 A 租户的凭据写进 B 租户，
且事后无法识别。

state 落 PostgreSQL（ADR 0009 D6）：多实例下实例 A 签发、回调可能打到实例 B，
文件方案必然失败。一次性由 ``DELETE … RETURNING`` 保证。
"""

from __future__ import annotations

import re
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from mybmo_mcp.auth.models import (
    STATUS_ACTIVE,
    STATUS_PENDING,
    Credential,
    validate_alias,
)
from mybmo_mcp.auth.oauth import PLATFORM_ID, BaiduOAuth, build_authorize_url
from mybmo_mcp.auth.tenant_context import TenantContext
from mybmo_mcp.baidu.errors import CredentialError

STATE_TTL = 900.0
#: nonce 直接进 URL 与数据库主键，只允许 URL 安全字符；长度下限挡住弱随机。
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


class PgBindingStateStore:
    """一次性 state 落 PostgreSQL。进程重启不丢，取用即删除。

    **一次性靠 ``DELETE … RETURNING``**：并发消费同一个 nonce 时，只有一个请求能
    拿到行（行锁 + 删除），另一个拿到空——不需要额外的"已用"标记。
    """

    def __init__(
        self,
        dsn: str,
        *,
        ttl: float = STATE_TTL,
        now=time.time,
        connect: Callable[[], Any] | None = None,
    ) -> None:
        if not dsn:
            raise CredentialError("PgBindingStateStore 需要 MYBMO_DATABASE_URL")
        self._dsn = dsn
        self._ttl = ttl
        self._now = now
        self._connect_override = connect

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        if self._connect_override is not None:
            conn = self._connect_override()
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
            return

        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            yield conn

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
        with self._conn() as conn:
            # 顺手清过期行：签发是低频动作，代价可忽略，省掉一个定时任务
            conn.execute(
                "DELETE FROM binding_states WHERE expires_at <= now()"
            )
            conn.execute(
                "INSERT INTO binding_states"
                " (nonce, tenant_id, subject, alias, app_id, issued_at, expires_at)"
                " VALUES (%s, %s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s))",
                (
                    state.nonce,
                    tenant,
                    subject,
                    alias,
                    app_id,
                    state.issued_at,
                    state.expires_at,
                ),
            )
        return state

    def consume(self, nonce: str) -> BindingState | None:
        """取用并销毁。返回 ``None`` 表示不存在、已用过或已过期。"""
        if not nonce or not _NONCE_RE.match(nonce):
            return None
        with self._conn() as conn:
            row = conn.execute(
                "DELETE FROM binding_states"
                " WHERE nonce = %s AND expires_at > now()"
                " RETURNING nonce, tenant_id, subject, alias, app_id,"
                " extract(epoch FROM issued_at) AS issued_at,"
                " extract(epoch FROM expires_at) AS expires_at",
                (nonce,),
            ).fetchone()
        if row is None:
            return None
        return BindingState(
            nonce=str(row["nonce"]),
            subject=str(row["subject"]),
            tenant=str(row["tenant_id"]),
            alias=str(row["alias"]),
            issued_at=float(row["issued_at"] or 0.0),
            expires_at=float(row["expires_at"] or 0.0),
            app_id=str(row["app_id"] or ""),
        )


class BindingManager:
    """把「签发授权链接」与「回调落库」串起来。

    多租户之后，绑定必须知道"落到哪个租户"：签发时由调用方给 ``tenant``，
    回调时**只认 state 里的 tenant**——浏览器那一跳没有令牌，state 是唯一线索。
    """

    def __init__(
        self,
        states: PgBindingStateStore,
        *,
        redirect_uri: str,
        context_for: Callable[[str], TenantContext],
        platform_id: str = PLATFORM_ID,
        oauth: Any = None,
    ) -> None:
        self._states = states
        self._redirect_uri = redirect_uri
        self._context_for = context_for
        self._platform_id = platform_id
        #: 兼容/测试用：直接给一个 oauth 实例（生产按应用现造）
        self._oauth = oauth

    # ---------- 签发 ----------

    def authorize_url(
        self, *, subject: str, tenant: str, alias: str, app_id: str = ""
    ) -> tuple[str, BindingState]:
        alias = validate_alias(alias)
        ctx = self._context_for(tenant)
        resolved_app = (app_id or "").strip()
        secret, scope = self._app_credentials(ctx, resolved_app)

        # 预写占位凭据：state 里只放 alias 与 app_id，密钥从这条记录取回，
        # 不往 state 文件里塞明文密钥。
        ctx.store.save(
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

        **state 是租户的唯一来源**：回调这一跳来自浏览器，没有令牌可依据。
        state 缺失/过期一律失败——猜一个租户等于把 A 租户的凭据写进 B 租户，
        且事后无法识别（ADR 0008 D5）。``app_id`` 参数只为日志留痕，不参与判定。
        """
        resolved_state = self._states.consume(state) if state else None
        if resolved_state is None:
            raise CredentialError(
                "授权链接已失效（state 缺失或已被使用），无法确定这次绑定属于哪个租户；"
                "请重新发起一次绑定"
            )
        if not resolved_state.tenant:
            raise CredentialError(
                "绑定 state 里没有租户信息，拒绝落库（多租户下不能猜租户）；"
                "请用 auth bind --tenant <租户> 重新发起"
            )

        ctx = self._context_for(resolved_state.tenant)
        alias = resolved_state.alias
        resolved_app = resolved_state.app_id or app_id
        secret = self._pending_secret(ctx, alias, resolved_app)

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
            ctx, alias=alias, app_id=resolved_app, baidu_user_id=resolved_user_id
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
            status=STATUS_ACTIVE,
            note="",
        )
        ctx.store.save(credential)
        # 归属登记失败不能吞掉：凭据已经落库，但账户会因此对所有人不可见，
        # 必须在回调页面上让操作者看到（由调用方把状态回显出去）。
        ctx.tenancy.register_account(
            credential.alias,
            owner=resolved_state.subject,
            user_name=credential.user_name,
        )
        return credential, resolved_state

    # ---------- 内部 ----------

    def _app_credentials(self, ctx: TenantContext, app_id: str) -> tuple[str, str]:
        """返回 ``(secret_key, scope)``。查不到就报错并告诉用户怎么登记。"""
        if not app_id:
            raise CredentialError(
                "缺少应用 ID（appId）。绑定必须指定应用，见 list_apps 或 mybmo-mcp app secret list"
            )
        app = ctx.app_secrets.get(app_id)
        if app is not None and app.secret_key:
            return app.secret_key, app.scope
        raise CredentialError(
            f"应用 {app_id} 未登记密钥。先在服务器上执行："
            f"mybmo-mcp app secret set {app_id}（密钥为隐藏输入，不会进入对话）"
        )

    def _pending_secret(self, ctx: TenantContext, alias: str, app_id: str) -> str:
        """优先用占位凭据里存的那把密钥；没有就回落到应用登记表。"""
        try:
            credential = ctx.store.load(alias)
        except CredentialError:
            credential = None
        if (
            credential is not None
            and credential.app_id == app_id
            and credential.secret_key
        ):
            return credential.secret_key
        secret, _ = self._app_credentials(ctx, app_id)
        return secret

    def _oauth_for(self, app_id: str, secret_key: str) -> tuple[Any, bool]:
        """返回 ``(oauth, 是否由这里创建)``。创建的要负责关闭。"""
        if self._oauth is not None:
            return self._oauth, False
        return BaiduOAuth(app_id, secret_key), True

    def _assert_no_conflict(
        self, ctx: TenantContext, *, alias: str, app_id: str, baidu_user_id: int
    ) -> None:
        """ADR 0006 D6：一个百度账户只绑一个应用、一个应用只服务一个账户。

        PG 后端的唯一索引会兜住，但文件后端没有约束，所以这里做一次显式检查，
        让两个后端的行为一致；冲突时**拒绝而不是覆盖**，避免一组授权无声失效。
        """
        try:
            aliases = ctx.store.aliases()
        except CredentialError:
            aliases = []
        for other in aliases:
            if other == alias:
                continue
            try:
                credential = ctx.store.load(other)
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
