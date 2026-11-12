from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from mybmo_mcp.auth.models import STATUS_ACTIVE, STATUS_REAUTH_REQUIRED, Credential
from mybmo_mcp.auth.oauth import TokenBundle
from mybmo_mcp.auth.registry import AccountRegistry
from mybmo_mcp.auth.service import CredentialService
from mybmo_mcp.auth.vault import FileVault
from mybmo_mcp.baidu.errors import CredentialError

KEY = b"k" * 32


class FakeOAuth:
    def __init__(self, *, bundle: TokenBundle | None = None, error: Exception | None = None):
        self.calls = 0
        self.bundle = bundle or TokenBundle(
            access_token="new-at", refresh_token="new-rt", expires_at=time.time() + 86400
        )
        self.error = error

    async def refresh(self, refresh_token: str, user_id: int) -> TokenBundle:
        self.calls += 1
        await asyncio.sleep(0.01)
        if self.error is not None:
            raise self.error
        return self.bundle


def setup_store(
    tmp_path: Path, *, expires_at: float | None = None, refresh_token: str = "rt-1"
) -> tuple[FileVault, AccountRegistry]:
    vault = FileVault(tmp_path / "data", KEY)
    vault.save(
        Credential(
            alias="axd",
            user_name="示例账户",
            user_id=1001,
            access_token="old-at",
            refresh_token=refresh_token,
            expires_at=expires_at if expires_at is not None else time.time() - 10,
        )
    )
    return vault, AccountRegistry(vault, env={})


def service(registry: AccountRegistry, oauth: FakeOAuth, tmp_path: Path) -> CredentialService:
    return CredentialService(
        registry,
        oauth,  # type: ignore[arg-type]
        lock_dir=tmp_path / "locks",
        oauth_for=lambda _cred: oauth,  # type: ignore[arg-type,return-value]
    )


async def test_fresh_credential_is_returned_without_refresh(tmp_path: Path) -> None:
    _, registry = setup_store(tmp_path, expires_at=time.time() + 3600)
    oauth = FakeOAuth()

    credential = await service(registry, oauth, tmp_path).ensure_valid("axd")

    assert credential.access_token == "old-at"
    assert oauth.calls == 0


async def test_expired_credential_is_refreshed_and_persisted(tmp_path: Path) -> None:
    vault, registry = setup_store(tmp_path)
    oauth = FakeOAuth()

    credential = await service(registry, oauth, tmp_path).ensure_valid("axd")

    assert credential.access_token == "new-at"
    assert oauth.calls == 1
    assert vault.load("axd").access_token == "new-at"
    assert vault.load("axd").status == STATUS_ACTIVE


async def test_refresh_failure_marks_reauth_required(tmp_path: Path) -> None:
    vault, registry = setup_store(tmp_path)
    oauth = FakeOAuth(error=CredentialError("refreshToken 已被拒绝"))

    with pytest.raises(CredentialError):
        await service(registry, oauth, tmp_path).ensure_valid("axd")

    stored = vault.load("axd")
    assert stored.status == STATUS_REAUTH_REQUIRED
    assert stored.access_token == "old-at"


async def test_reauth_required_is_not_retried(tmp_path: Path) -> None:
    vault, registry = setup_store(tmp_path)
    vault.save(vault.load("axd").mark_reauth_required("手动标记"))
    oauth = FakeOAuth()

    with pytest.raises(CredentialError, match="需要重新绑定"):
        await service(registry, oauth, tmp_path).ensure_valid("axd")

    assert oauth.calls == 0


async def test_concurrent_refresh_only_calls_oauth_once(tmp_path: Path) -> None:
    _, registry = setup_store(tmp_path)
    oauth = FakeOAuth()
    svc = service(registry, oauth, tmp_path)

    first, second = await asyncio.gather(svc.ensure_valid("axd"), svc.ensure_valid("axd"))

    assert oauth.calls == 1
    assert first.access_token == "new-at"
    assert second.access_token == "new-at"


async def test_missing_refresh_token_requires_rebinding(tmp_path: Path) -> None:
    _, registry = setup_store(tmp_path, refresh_token="")
    oauth = FakeOAuth()

    with pytest.raises(CredentialError, match="没有 refreshToken"):
        await service(registry, oauth, tmp_path).ensure_valid("axd")


async def test_environment_credential_is_not_persisted(tmp_path: Path) -> None:
    vault = FileVault(tmp_path / "data", KEY)
    env = {
        "BAIDU_USER_NAME": "临时账户",
        "BAIDU_ACCESS_TOKEN": "old-at",
        "BAIDU_REFRESH_TOKEN": "rt-1",
        "BAIDU_EXPIRES_AT": str(time.time() - 10),
    }
    registry = AccountRegistry(vault, env=env)

    credential = await service(registry, FakeOAuth(), tmp_path).ensure_valid("env")

    assert credential.access_token == "new-at"
    assert vault.scan() == ([], [])


async def test_credentials_for_requires_user_name(tmp_path: Path) -> None:
    vault = FileVault(tmp_path / "data", KEY)
    vault.save(Credential(alias="axd", access_token="at", expires_at=time.time() + 3600))
    registry = AccountRegistry(vault, env={})

    with pytest.raises(CredentialError, match="81020"):
        await service(registry, FakeOAuth(), tmp_path).credentials_for("axd")


async def test_credentials_for_returns_request_scoped_pair(tmp_path: Path) -> None:
    _, registry = setup_store(tmp_path, expires_at=time.time() + 3600)

    pair = await service(registry, FakeOAuth(), tmp_path).credentials_for("axd")

    assert pair.user_name == "示例账户"
    assert pair.access_token == "old-at"
    assert "old-at" not in repr(pair)


async def test_refresh_uses_the_credentials_own_app_credentials(tmp_path: Path) -> None:
    """ADR 0006 D5：刷新必须用这条凭据自己的 app_id/secret_key。

    回归用例。旧实现调全局单例，一旦凭据不是用环境变量那组授权的，
    refreshToken 就换不出令牌，还会被误标成 reauth_required。
    """
    vault = FileVault(tmp_path / "data", KEY)
    vault.save(
        Credential(
            alias="axd",
            user_name="示例账户",
            user_id=1001,
            app_id="app-own",
            secret_key="sk-own",
            access_token="old-at",
            refresh_token="rt-1",
            expires_at=time.time() - 10,
        )
    )
    registry = AccountRegistry(vault, env={})
    seen: list[str] = []
    fake = FakeOAuth()

    def factory(credential: Credential) -> FakeOAuth:
        seen.append(f"{credential.app_id}/{credential.secret_key}")
        return fake

    svc = CredentialService(  # type: ignore[arg-type]
        registry, fake, lock_dir=tmp_path / "locks", oauth_for=factory
    )
    await svc.ensure_valid("axd")

    assert seen == ["app-own/sk-own"]


def test_default_oauth_factory_uses_credential_app_id(tmp_path: Path) -> None:
    _, registry = setup_store(tmp_path)
    svc = CredentialService(registry, FakeOAuth(), lock_dir=tmp_path / "locks")  # type: ignore[arg-type]

    built = svc._oauth_for_credential(
        Credential(alias="axd", app_id="app-own", secret_key="sk-own")
    )

    assert built._app_id == "app-own"


def test_default_oauth_factory_falls_back_when_credential_has_no_app(tmp_path: Path) -> None:
    """历史凭据没存应用凭据时，退回全局那一组，不能直接炸。"""
    _, registry = setup_store(tmp_path)
    fallback = FakeOAuth()
    svc = CredentialService(registry, fallback, lock_dir=tmp_path / "locks")  # type: ignore[arg-type]

    assert svc._oauth_for_credential(Credential(alias="axd")) is fallback


def test_refresh_writes_back_inside_the_row_lock(tmp_path: Path) -> None:
    """PG 后端：写回必须走行锁句柄，另开连接会等自己持有的锁而死锁。"""
    from contextlib import contextmanager

    vault = FileVault(tmp_path / "data", KEY)
    vault.save(
        Credential(
            alias="axd",
            user_name="示例账户",
            user_id=1001,
            app_id="app-own",
            secret_key="sk-own",
            access_token="old-at",
            refresh_token="rt-1",
            expires_at=time.time() - 10,
        )
    )

    class LockedHolder:
        def __init__(self, credential: Credential) -> None:
            self.credential = credential
            self.saved: list[Credential] = []

        def save(self, credential: Credential) -> None:
            self.saved.append(credential)
            vault.save(credential)

    saved_via_lock: list[Credential] = []

    class LockedVault:
        """给 FileVault 加一个 locked()，模拟 PG 后端的事务句柄。"""

        def __getattr__(self, name: str) -> object:
            return getattr(vault, name)

        @contextmanager
        def locked(self, alias: str):
            holder = LockedHolder(vault.load(alias))
            yield holder
            saved_via_lock.extend(holder.saved)

    store = LockedVault()
    registry = AccountRegistry(store, env={})  # type: ignore[arg-type]
    fake = FakeOAuth()
    svc = CredentialService(  # type: ignore[arg-type]
        registry, fake, lock_dir=tmp_path / "locks", oauth_for=lambda _c: fake
    )

    updated = asyncio.run(svc.ensure_valid("axd"))

    assert updated.access_token == "new-at"
    assert [cred.alias for cred in saved_via_lock] == ["axd"]
