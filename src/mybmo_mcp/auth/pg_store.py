"""PostgreSQL 版凭据存储。

与 :class:`mybmo_mcp.auth.vault.FileVault` **同接口**（见 ``auth/store.py`` 的
``CredentialStore`` 协议），调用方零改动；区别是数据在库里、不落文件。

设计见 ``docs/decisions/0007-credentials-in-postgres.md``：

* 明文列只放要查询/要约束的字段（``app_id`` / ``user_name`` / ``baidu_user_id`` /
  ``status`` / 过期时间），令牌与应用密钥**只存在 ``cipher`` 里**
* 整块 AES-GCM，AAD 带上 ``tenant_id``——落库后"行"更容易被搬动，
  AAD 绑租户可以挡住跨租户复制密文（文件后端沿用 v1 AAD，不动存量）
* 两个部分唯一索引落实 ADR 0006 D2 的 1:1 约束；冲突在这里翻译成
  ``CredentialError``，上层不必认识 psycopg 的异常类型
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from mybmo_mcp.auth import crypto
from mybmo_mcp.auth.models import Credential, validate_alias
from mybmo_mcp.baidu.errors import CredentialError

#: v2：AAD 带上租户——落库后"行"更容易被搬动，绑租户能挡住跨租户复制密文
AAD_PREFIX = "mybmo-mcp/credential/v2/"
KEY_VERSION = 1


def _aad(tenant_id: str, alias: str) -> bytes:
    return crypto.aad(AAD_PREFIX, tenant_id, alias)


class PgCredentialStore:
    """凭据存在 PostgreSQL 的 ``credentials`` 表里的实现。"""

    def __init__(
        self,
        dsn: str,
        master_key: bytes,
        *,
        tenant_id: str,
        connect: Callable[[], Any] | None = None,
    ) -> None:
        if not dsn:
            raise CredentialError("PgCredentialStore 需要 MYBMO_DATABASE_URL")
        self._dsn = dsn
        self._tenant_id = tenant_id
        self._aead = crypto.aead(master_key)
        self._connect_override = connect

    # ---------- 连接 ----------

    @contextmanager
    def _conn(self, *, autocommit: bool = False) -> Iterator[Any]:
        if self._connect_override is not None:
            conn = self._connect_override()
            try:
                yield conn
                if not autocommit:
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

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    @property
    def connect_override(self) -> Callable[[], Any] | None:
        """测试用：拿到注入的连接工厂，便于另建一个 store 验证 AAD 绑租户。"""
        return self._connect_override

    # ---------- 读写 ----------

    def save(self, credential: Credential) -> None:
        alias, sql, params = self._prepare(credential)
        with self._conn() as conn:
            self._execute(conn, sql, params, alias)

    def _prepare(self, credential: Credential) -> tuple[str, str, dict[str, Any]]:
        alias = validate_alias(credential.alias)
        nonce, cipher = crypto.seal(
            self._aead, credential.to_storage(), _aad(self._tenant_id, alias)
        )

        sql = """
            INSERT INTO credentials (
                tenant_id, alias, app_id, user_name, baidu_user_id, status,
                expires_at, refresh_expires_at, nonce, cipher, key_version, updated_at
            ) VALUES (
                %(tenant)s, %(alias)s, %(app_id)s, %(user_name)s, %(user_id)s, %(status)s,
                to_timestamp(NULLIF(%(expires_at)s, 0)),
                to_timestamp(NULLIF(%(refresh_expires_at)s, 0)),
                %(nonce)s, %(cipher)s, %(key_version)s, now()
            )
            ON CONFLICT (tenant_id, alias) DO UPDATE SET
                app_id             = EXCLUDED.app_id,
                user_name          = EXCLUDED.user_name,
                baidu_user_id      = EXCLUDED.baidu_user_id,
                status             = EXCLUDED.status,
                expires_at         = EXCLUDED.expires_at,
                refresh_expires_at = EXCLUDED.refresh_expires_at,
                nonce              = EXCLUDED.nonce,
                cipher             = EXCLUDED.cipher,
                key_version        = EXCLUDED.key_version,
                updated_at         = now()
        """
        params = {
            "tenant": self._tenant_id,
            "alias": alias,
            "app_id": credential.app_id,
            "user_name": credential.user_name,
            "user_id": credential.user_id,
            "status": credential.status,
            "expires_at": credential.expires_at,
            "refresh_expires_at": credential.refresh_expires_at,
            "nonce": nonce,
            "cipher": cipher,
            "key_version": KEY_VERSION,
        }
        return alias, sql, params

    def _execute(self, conn: Any, sql: str, params: dict[str, Any], alias: str) -> None:
        try:
            conn.execute(sql, params)
        except Exception as exc:  # 按异常类型翻译唯一约束冲突
            raise self._translate(exc, alias) from exc

    def load(self, alias: str) -> Credential:
        alias = validate_alias(alias)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT nonce, cipher FROM credentials WHERE tenant_id = %s AND alias = %s",
                (self._tenant_id, alias),
            ).fetchone()
        if row is None:
            raise CredentialError(
                f"账户不存在：{alias}（租户 {self._tenant_id} 的 credentials 表里没有这条）"
            )
        return self._decrypt_row(row, alias)

    def delete(self, alias: str) -> None:
        alias = validate_alias(alias)
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM credentials WHERE tenant_id = %s AND alias = %s",
                (self._tenant_id, alias),
            )

    # ---------- 枚举 ----------

    def aliases(self) -> list[str]:
        return self.scan()[0]

    def scan(self) -> tuple[list[str], list[tuple[Any, str]]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT alias, nonce, cipher FROM credentials WHERE tenant_id = %s ORDER BY alias",
                (self._tenant_id,),
            ).fetchall()

        aliases: list[str] = []
        errors: list[tuple[Any, str]] = []
        for row in rows:
            alias = str(row["alias"])
            try:
                self._decrypt_row(row, alias)
            except CredentialError as exc:
                errors.append((f"credentials:{self._tenant_id}/{alias}", str(exc)))
                continue
            aliases.append(alias)
        return aliases, errors

    # ---------- 默认账户 ----------

    def default_alias(self) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT alias FROM credentials WHERE tenant_id = %s AND is_default",
                (self._tenant_id,),
            ).fetchone()
        return str(row["alias"]) if row else None

    def set_default(self, alias: str) -> None:
        alias = validate_alias(alias)
        if not self._exists(alias):
            raise CredentialError(f"账户不存在，无法设为默认：{alias}")
        with self._conn() as conn:
            conn.execute(
                "UPDATE credentials SET is_default = (alias = %s) WHERE tenant_id = %s",
                (alias, self._tenant_id),
            )

    # ---------- 事务性操作（文件后端做不到，故不进协议） ----------

    @contextmanager
    def locked(self, alias: str) -> Iterator[LockedCredential]:
        """在事务内锁住一行并交出可写句柄，供刷新使用（ADR 0007 D4）。

        百度每次刷新都会轮换 ``refreshToken``，两个并发刷新会让其中一方
        拿到已作废的令牌。``SELECT … FOR UPDATE`` 把同一账户的刷新串起来；
        锁只覆盖单行，不影响其他账户。

        句柄的 ``save()`` 必须写在**同一个事务**里——另开连接会等自己持有的行锁。
        """
        alias = validate_alias(alias)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT nonce, cipher FROM credentials "
                "WHERE tenant_id = %s AND alias = %s FOR UPDATE",
                (self._tenant_id, alias),
            ).fetchone()
            if row is None:
                raise CredentialError(
                    f"账户不存在：{alias}（租户 {self._tenant_id} 的 credentials 表里没有这条）"
                )
            yield LockedCredential(self, conn, alias, self._decrypt_row(row, alias))

    def _exists(self, alias: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM credentials WHERE tenant_id = %s AND alias = %s",
                (self._tenant_id, alias),
            ).fetchone()
        return row is not None

    # ---------- 内部 ----------

    def _decrypt_row(self, row: Any, alias: str) -> Credential:
        data = crypto.open_json(
            self._aead,
            bytes(row["nonce"]),
            bytes(row["cipher"]),
            _aad(self._tenant_id, alias),
            label=f"凭据 {self._tenant_id}/{alias}",
        )
        try:
            return Credential.from_storage(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise CredentialError(
                f"凭据损坏：{self._tenant_id}/{alias} 缺少必需字段或字段类型错误"
            ) from exc

    def _translate(self, exc: Exception, alias: str) -> CredentialError:
        """把唯一约束冲突翻译成能指导操作的中文（ADR 0006 D6）。"""
        name = type(exc).__name__
        text = str(exc)
        if name == "UniqueViolation":
            if "idx_credentials_user" in text:
                return CredentialError(
                    f"该百度账户在本租户内已绑定过其他应用（本次写入 alias={alias}）。"
                    "本项目规定一个百度账户只能绑定一个应用，如需换用其他应用，"
                    "请先解绑原有账户"
                )
            if "idx_credentials_app" in text:
                return CredentialError(
                    "该应用已绑定过其他百度账户。一个应用只能服务一个账户，"
                    "如需换绑请先解绑原有账户"
                )
            if "idx_credentials_default" in text:
                return CredentialError("默认账户设置冲突：同一租户只能有一个默认账户")
        return CredentialError(f"凭据写入失败：{name}")


class LockedCredential:
    """``locked()`` 事务内的一条凭据。只能通过 ``save()`` 写回同一事务。"""

    def __init__(
        self, store: PgCredentialStore, conn: Any, alias: str, credential: Credential
    ) -> None:
        self._store = store
        self._conn = conn
        self._alias = alias
        self._credential = credential

    @property
    def credential(self) -> Credential:
        return self._credential

    def save(self, credential: Credential) -> None:
        alias, sql, params = self._store._prepare(credential)
        self._store._execute(self._conn, sql, params, alias)
