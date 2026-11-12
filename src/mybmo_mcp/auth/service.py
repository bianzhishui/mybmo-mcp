"""凭据服务：按需刷新令牌，并保证「新令牌先落库再使用」。

顺序很重要：**先落盘，再返回**。如果先返回再落盘，进程在这两步之间崩掉，
磁盘上留着已被轮换掉的旧 refreshToken，下次启动直接 `reauth_required`。

刷新失败的处理是「标记后失败」：把账户标成 ``reauth_required`` 并抛出，
而不是继续拿旧令牌去请求。旧令牌已经失效，重试只会换来一串 ``81020``。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from mybmo_mcp.auth import lock
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
        oauth: BaiduOAuth,
        *,
        lock_dir: str | Path,
        lock_timeout: float = lock.DEFAULT_TIMEOUT,
        now=time.time,
        oauth_for: Callable[[Credential], BaiduOAuth] | None = None,
    ) -> None:
        self._registry = registry
        self._oauth = oauth
        self._lock_dir = Path(lock_dir)
        self._lock_timeout = lock_timeout
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

        # PG 后端有行锁就优先用它：文件锁只在同一台机器内有效，多副本部署挡不住并发刷新
        locked = getattr(self._registry.store, "locked", None)
        async with lock.exclusive(self._lock_path(credential.alias), timeout=self._lock_timeout):
            if locked is None:
                # 拿到锁后重新读一次：排队期间别的进程可能已经刷过了。
                fresh = self._registry.get(credential.alias)
                if not fresh.needs_refresh(self._now()):
                    return fresh
                return await self._refresh(fresh, self._persist)

            with locked(credential.alias) as row:
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
        try:
            bundle = await oauth.refresh(fresh.refresh_token, fresh.user_id)
        except CredentialError as exc:
            persist(fresh.mark_reauth_required(str(exc)))
            raise
        finally:
            if oauth is not self._oauth:
                await oauth.aclose()

        updated = fresh.with_tokens(
            access_token=bundle.access_token,
            refresh_token=bundle.refresh_token or None,
            expires_at=bundle.expires_at or None,
            refresh_expires_at=bundle.refresh_expires_at or None,
            open_id=bundle.open_id or None,
            user_id=bundle.user_id or None,
            scope=bundle.scope or None,
        )
        persist(updated)
        return updated

    def _oauth_for_credential(self, credential: Credential) -> BaiduOAuth:
        """刷新必须用**这条凭据自己的**应用密钥（ADR 0006 D5）。

        旧实现用全局单例，忽略 ``credential.app_id``：只要凭据不是用环境变量那组
        授权的，``refreshToken`` 就换不出令牌，还会被误标成 ``reauth_required``，
        用户只能反复重绑。
        """
        if credential.app_id and credential.secret_key:
            return BaiduOAuth(credential.app_id, credential.secret_key)
        # 兼容：凭据里没有应用凭据的历史数据，退回全局那一组
        return self._oauth

    async def _resolve(self, alias: str | None) -> Credential:
        return self._registry.resolve(alias)

    def _persist(self, credential: Credential) -> None:
        """环境变量提供的临时凭据不落盘，其余一律写回。"""
        if self._registry.is_environment(credential.alias):
            return
        self._registry.store.save(credential)

    def _lock_path(self, alias: str) -> Path:
        return self._lock_dir / f"{alias}.lock"
