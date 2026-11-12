"""应用密钥（appId / secretKey）登记。

**为什么单独一层**：绑定要先知道用哪个应用、哪把密钥，才能生成授权链接；而那时
别名（alias）还没落库、百度账户还不知道。所以应用密钥必须能按 ``app_id`` 独立登记，
不能只塞在凭据行里。

密钥进不来对话：只经 CLI ``mybmo-mcp app secret set <app_id>`` 用隐藏输入写入，
MCP 工具层只接受 ``appId``（见 ADR 0006 D3 的折中方案）。

两个后端与凭据后端保持同一套（ADR 0007 D3）：
``file`` → ``data/apps/<app_id>.json``；``pg`` → ``app_secrets`` 表。
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from mybmo_mcp.auth import crypto
from mybmo_mcp.baidu.errors import CredentialError

#: 应用 ID 是百度开发者中心给的 32 位十六进制串，但也可能是别的形态。
#: 限制成 URL/文件名安全的字符，同样挡住路径穿越。
APP_ID_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"
_APP_ID_RE = re.compile(APP_ID_PATTERN)

ENVELOPE_VERSION = 1
AAD_PREFIX = "mybmo-mcp/app/v1/"
APPS_DIRNAME = "apps"
_DIR_MODE = crypto.DIR_MODE
_FILE_MODE = crypto.FILE_MODE


def validate_app_id(app_id: str) -> str:
    if not isinstance(app_id, str) or not _APP_ID_RE.match(app_id):
        raise CredentialError(
            f"应用 ID 不合法：{app_id!r}。要求匹配 {APP_ID_PATTERN}"
        )
    return app_id


def _aad(app_id: str) -> bytes:
    return crypto.aad(AAD_PREFIX, app_id)


@dataclass(frozen=True, slots=True)
class AppSecret:
    """一个应用的一组密钥。``secret_key`` 永不出现在 repr 里。"""

    app_id: str
    secret_key: str = field(default="", repr=False)
    scope: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        validate_app_id(self.app_id)

    def to_public(self) -> dict[str, Any]:
        """对外展示：只有 app_id 与备注，**不含密钥**。"""
        return {
            "app_id": self.app_id,
            "scope": self.scope,
            "note": self.note,
            "has_secret": bool(self.secret_key),
        }


@runtime_checkable
class AppSecretStore(Protocol):
    def save(self, app: AppSecret) -> None: ...

    def get(self, app_id: str) -> AppSecret | None: ...

    def delete(self, app_id: str) -> None: ...

    def list(self) -> list[AppSecret]: ...


# ---------------------------------------------------------------- 文件后端


class FileAppSecretStore:
    """``data/apps/<app_id>.json``，AES-GCM，AAD 绑 app_id。"""

    def __init__(self, root: str | os.PathLike[str], master_key: bytes) -> None:
        self.root = Path(root)
        self.apps_dir = self.root / APPS_DIRNAME
        self._aead = crypto.aead(master_key)
        self.apps_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, _DIR_MODE)
        os.chmod(self.apps_dir, _DIR_MODE)

    def path_for(self, app_id: str) -> Path:
        return self.apps_dir / f"{validate_app_id(app_id)}.json"

    def save(self, app: AppSecret) -> None:
        validate_app_id(app.app_id)
        payload = {
            "app_id": app.app_id,
            "secret_key": app.secret_key,
            "scope": app.scope,
            "note": app.note,
        }
        nonce, ciphertext = crypto.seal(self._aead, payload, _aad(app.app_id))
        envelope = {
            "v": ENVELOPE_VERSION,
            "app_id": app.app_id,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        crypto.atomic_write(
            self.path_for(app.app_id),
            json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
        )

    def get(self, app_id: str) -> AppSecret | None:
        path = self.path_for(app_id)
        if not path.is_file():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            nonce = base64.b64decode(envelope["nonce"], validate=True)
            ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CredentialError(f"应用密钥无法读取：{path}") from exc
        data = crypto.open_json(
            self._aead, nonce, ciphertext, _aad(app_id), label=f"应用密钥 {path}"
        )
        return AppSecret(
            app_id=app_id,
            secret_key=str(data.get("secret_key", "")),
            scope=str(data.get("scope", "")),
            note=str(data.get("note", "")),
        )

    def delete(self, app_id: str) -> None:
        self.path_for(app_id).unlink(missing_ok=True)

    def list(self) -> list[AppSecret]:
        found: list[AppSecret] = []
        for path in sorted(self.apps_dir.glob("*.json")):
            app = self.get(path.stem)
            if app is not None:
                found.append(app)
        return found


# ---------------------------------------------------------------- PG 后端


class PgAppSecretStore:
    """``app_secrets`` 表，AAD 与文件后端一致（只绑 app_id）。"""

    def __init__(
        self,
        dsn: str,
        master_key: bytes,
        *,
        tenant_id: str,
        connect: Callable[[], Any] | None = None,
    ) -> None:
        if not dsn:
            raise CredentialError("PgAppSecretStore 需要 MYBMO_DATABASE_URL")
        self._dsn = dsn
        self._tenant_id = tenant_id
        self._aead = crypto.aead(master_key)
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

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    def save(self, app: AppSecret) -> None:
        validate_app_id(app.app_id)
        payload = {
            "app_id": app.app_id,
            "secret_key": app.secret_key,
            "scope": app.scope,
            "note": app.note,
        }
        nonce, cipher = crypto.seal(self._aead, payload, _aad(app.app_id))
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO app_secrets (tenant_id, app_id, nonce, cipher, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (tenant_id, app_id) DO UPDATE SET
                    nonce = EXCLUDED.nonce,
                    cipher = EXCLUDED.cipher,
                    updated_at = now()
                """,
                (self._tenant_id, app.app_id, nonce, cipher),
            )

    def get(self, app_id: str) -> AppSecret | None:
        validate_app_id(app_id)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT nonce, cipher FROM app_secrets WHERE tenant_id = %s AND app_id = %s",
                (self._tenant_id, app_id),
            ).fetchone()
        if row is None:
            return None
        return self._decrypt(row, app_id)

    def delete(self, app_id: str) -> None:
        validate_app_id(app_id)
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM app_secrets WHERE tenant_id = %s AND app_id = %s",
                (self._tenant_id, app_id),
            )

    def list(self) -> list[AppSecret]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT app_id, nonce, cipher FROM app_secrets"
                " WHERE tenant_id = %s ORDER BY app_id",
                (self._tenant_id,),
            ).fetchall()
        return [self._decrypt(row, str(row["app_id"])) for row in rows]

    def _decrypt(self, row: Any, app_id: str) -> AppSecret:
        data = crypto.open_json(
            self._aead,
            bytes(row["nonce"]),
            bytes(row["cipher"]),
            _aad(app_id),
            label=f"应用密钥 {self._tenant_id}/{app_id}",
        )
        return AppSecret(
            app_id=app_id,
            secret_key=str(data.get("secret_key", "")),
            scope=str(data.get("scope", "")),
            note=str(data.get("note", "")),
        )
