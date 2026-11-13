"""凭据服务：按需刷新令牌，并保证「新令牌先落库再使用」。

顺序很重要：**先落盘，再返回**。如果先返回再落盘，进程在这两步之间崩掉，
磁盘上留着已被轮换掉的旧 refreshToken，下次启动直接 `reauth_required`。

刷新失败的处理是「标记后失败」：把账户标成 ``reauth_required`` 并抛出，
而不是继续拿旧令牌去请求。旧令牌已经失效，重试只会换来一串 ``81020``。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace

from mybmo_mcp.auth.models import STATUS_REAUTH_REQUIRED, Credential
from mybmo_mcp.auth.oauth import BaiduOAuth
from mybmo_mcp.auth.registry import AccountRegistry
from mybmo_mcp.baidu.errors import CredentialError
from mybmo_mcp.baidu.transport import RequestCredentials


class CredentialService:
    """把「账户注册表 + OAuth + 凭据库」组装成传输层可直接消费的凭据来源。"""

    def __init__(
        self,
        registry: AccountRegistry,
        oauth: BaiduOAuth | None,
        *,
        now=time.time,
        oauth_for: Callable[[Credential], BaiduOAuth] | None = None,
    ) -> None:
        self._registry = registry
        self._oauth = oauth
        self._now = now
        #: 测试注入点：默认按凭据自带的 app_id/secret_key 现造客户端
        self._oauth_for = oauth_for or self._oauth_for_credential

    async def ensure_valid(self, alias: str | None = None) -> Credential:
        """返回可用的凭据。过期则刷新，必要时加锁串行。"""
        credential = await self._resolve(alias)
        if not credential.needs_refresh(self._now()):
            return credential

        if credential.status == STATUS_REAUTH_REQUIRED:
            raise CredentialError(
                f"账户 {credential.alias} 的授权已失效，需要重新绑定：{credential.note}"
            )
        if not credential.refresh_token:
            raise CredentialError(f"账户 {credential.alias} 没有 refreshToken，需要重新授权")
        if credential.refresh_expired(self._now()):
            self._persist(credential.mark_reauth_required("refreshToken 已过期（约 30 天）"))
            raise CredentialError(f"账户 {credential.alias} 的 refreshToken 已过期，需要重新授权")

        if self._registry.is_environment(credential.alias):
            # 环境变量提供的临时凭据不落库，刷新结果也不需要写回，跳过行锁
            return await self._refresh(credential, self._persist)

        # 行锁是唯一的并发防线：文件锁只在同一台机器内有效，多副本部署挡不住
        with self._registry.store.locked(credential.alias) as row:
            fresh = row.credential
            if not fresh.needs_refresh(self._now()):
                return fresh
            return await self._refresh(fresh, row.save)

    async def credentials_for(self, alias: str | None = None) -> RequestCredentials:
        """给传输层用：只交出请求必需的账户名与令牌。"""
        credential = await self.ensure_valid(alias)
        if not credential.user_name:
            raise CredentialError(
                f"账户 {credential.alias} 缺少 user_name（账户中文名），"
                "百度接口会返回 81020，需要重新绑定"
            )
        return RequestCredentials(
            user_name=credential.user_name, access_token=credential.access_token
        )

    # ---------- 内部 ----------

    async def _refresh(self, fresh: Credential, persist) -> Credential:
        """刷新一次并写回。``persist`` 由调用方给：文件后端是普通保存，
        PG 后端是**同一事务内**的写回（否则会等自己持有的行锁）。"""
        oauth = self._oauth_for(fresh)
        # **整个刷新流程都在这个 try 里**：``user_info`` 用的是同一个客户端，
        # 提前 aclose() 会让它抛 ``RuntimeError: Cannot send a request, as the client
        # has been closed``——而此时新令牌还没 persist，于是这个账户从此**每次调用都失败**
        # 且库里仍是旧 refreshToken（2026-11-13 线上实测：账户令牌一过期就永久失效）。
        try:
            try:
                bundle = await oauth.refresh(fresh.refresh_token, fresh.user_id)
            except CredentialError as exc:
                persist(fresh.mark_reauth_required(str(exc)))
                raise

            # 账户中文名会被人在百度后台改掉，而它是每次请求鉴权头里的 userName，
            # 过期后全部接口报 81020——偏偏表现为"授权失效"，很难排查。
            # 刷新时顺手校正一次，代价是每账户每天多一次 getUserInfo。
            user_name = fresh.user_name
            try:
                info = await oauth.user_info(
                    bundle.access_token,
                    user_id=bundle.user_id or fresh.user_id,
                    open_id=bundle.open_id or fresh.open_id,
                )
                fetched = str(info.get("masterName") or info.get("userName") or "").strip()
                if fetched:
                    user_name = fetched
            except (AttributeError, CredentialError):
                # 拿不到就沿用旧名：校正失败不能连累刷新本身
                pass

            updated = fresh.with_tokens(
                access_token=bundle.access_token,
                refresh_token=bundle.refresh_token or None,
                expires_at=bundle.expires_at or None,
                refresh_expires_at=bundle.refresh_expires_at or None,
                open_id=bundle.open_id or None,
                user_id=bundle.user_id or None,
                scope=bundle.scope or None,
            )
            if user_name and user_name != fresh.user_name:
                updated = replace(updated, user_name=user_name)
            persist(updated)
            return updated
        finally:
            # 只关自己造的客户端；注入的共享实例由 App 生命周期管
            if oauth is not self._oauth:
                await oauth.aclose()

    def _oauth_for_credential(self, credential: Credential) -> BaiduOAuth:
        """刷新必须用**这条凭据自己的**应用密钥（ADR 0006 D5）。

        旧实现用全局单例，忽略 ``credential.app_id``：只要凭据不是用环境变量那组
        授权的，``refreshToken`` 就换不出令牌，还会被误标成 ``reauth_required``，
        用户只能反复重绑。
        """
        if credential.app_id and credential.secret_key:
            return BaiduOAuth(credential.app_id, credential.secret_key)
        if self._oauth is None:
            # 多租户（ADR 0008）不再有"全局应用"；只有 local/file 或测试会注入它
            raise CredentialError(
                f"账户 {credential.alias} 没有自带应用凭据，而服务没有配置默认应用；"
                "请重新绑定（应用密钥已废弃服务级配置，见 ADR 0006 D1）"
            )
        return self._oauth

    async def _resolve(self, alias: str | None) -> Credential:
        return self._registry.resolve(alias)

    def _persist(self, credential: Credential) -> None:
        """环境变量提供的临时凭据不落盘，其余一律写回。"""
        if self._registry.is_environment(credential.alias):
            return
        self._registry.store.save(credential)
