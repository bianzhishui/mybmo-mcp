"""凭据库：AES-GCM 加密文件，一账户一文件，文件名即 alias。

设计要点：

- **AAD 绑定 alias**。加密时把 ``alias`` 作为附加认证数据，文件被改名后解密必然失败，
  杜绝「把 A 账户的凭据文件改名成 B，服务以为拿到了 B 的令牌」这种串号。
- **文件权限 600，目录 700**。加密之外再加一层文件系统保护。
- **原子写**。先写临时文件再 ``os.replace``，避免写一半断电留下半个 JSON。
- **损坏必须报错**。读取失败时给出具体路径与原因，绝不静默当成「没有这个账户」。

多租户生产环境改存 PostgreSQL ``bytea`` 列（每租户独立 DEK），接口保持一致。
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from mybmo_mcp.auth import crypto
from mybmo_mcp.auth.models import Credential, validate_alias
from mybmo_mcp.baidu.errors import CredentialError

ENVELOPE_VERSION = 1
#: 文件后端沿用 v1（AAD 只绑 alias，不带租户）——存量文件不重加密
AAD_PREFIX = "mybmo-mcp/credential/v1/"
ACCOUNTS_DIRNAME = "accounts"
DEFAULT_ALIAS_FILE = "default"
_DIR_MODE = crypto.DIR_MODE
_FILE_MODE = crypto.FILE_MODE


def _aad(alias: str) -> bytes:
    return crypto.aad(AAD_PREFIX, alias)


class FileVault:
    """本地加密凭据库。构造时会创建目录并收紧权限。"""

    def __init__(
        self,
        root: str | os.PathLike[str],
        master_key: bytes,
        *,
        accounts_dirname: str = ACCOUNTS_DIRNAME,
    ) -> None:
        self.root = Path(root)
        self.accounts_dir = self.root / accounts_dirname
        self._aead = crypto.aead(master_key)
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, _DIR_MODE)
        os.chmod(self.accounts_dir, _DIR_MODE)

    # ---------- 路径 ----------

    def path_for(self, alias: str) -> Path:
        return self.accounts_dir / f"{validate_alias(alias)}.json"

    # ---------- 读写 ----------

    def save(self, cred: Credential) -> None:
        path = self.path_for(cred.alias)
        nonce, ciphertext = crypto.seal(self._aead, cred.to_storage(), _aad(cred.alias))
        envelope = {
            "v": ENVELOPE_VERSION,
            "alias": cred.alias,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        crypto.atomic_write(path, json.dumps(envelope, ensure_ascii=False).encode("utf-8"))

    def load(self, alias: str) -> Credential:
        alias = validate_alias(alias)
        path = self.path_for(alias)
        if not path.is_file():
            raise CredentialError(f"账户不存在：{alias}（期望文件 {path}）")

        envelope = self._read_envelope(path)
        if envelope.get("alias") != alias:
            raise CredentialError(
                f"凭据文件损坏：{path} 内的 alias 是 {envelope.get('alias')!r}，"
                f"与文件名 {alias!r} 不一致"
            )

        plaintext = self._decrypt(envelope, alias, path)
        try:
            data = json.loads(plaintext)
        except ValueError as exc:
            raise CredentialError(f"凭据文件损坏：{path} 解密后的内容不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise CredentialError(f"凭据文件损坏：{path} 解密后的内容不是对象")
        try:
            return Credential.from_storage(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise CredentialError(f"凭据文件损坏：{path} 缺少必需字段或字段类型错误") from exc

    def delete(self, alias: str) -> None:
        path = self.path_for(alias)
        path.unlink(missing_ok=True)
        if self.default_alias() == alias:
            (self.root / DEFAULT_ALIAS_FILE).unlink(missing_ok=True)

    # ---------- 枚举 ----------

    def aliases(self) -> list[str]:
        """只返回能正常解密的 alias。损坏文件由 :meth:`scan` 单独报告。"""
        return self.scan()[0]

    def scan(self) -> tuple[list[str], list[tuple[Path, str]]]:
        """返回 ``(可用别名, [(路径, 原因)])``。

        损坏的文件不会从这里消失——调用方必须决定是报错还是跳过，
        而不是让「一个账户突然不见了」变成无声故障。
        """
        aliases: list[str] = []
        errors: list[tuple[Path, str]] = []
        for path in sorted(self.accounts_dir.glob("*.json")):
            try:
                self.load(path.stem)
            except CredentialError as exc:
                errors.append((path, str(exc)))
                continue
            aliases.append(path.stem)
        return aliases, errors

    # ---------- 默认账户 ----------

    def default_alias(self) -> str | None:
        path = self.root / DEFAULT_ALIAS_FILE
        if not path.is_file():
            return None
        alias = path.read_text(encoding="utf-8").strip()
        return alias or None

    def set_default(self, alias: str) -> None:
        alias = validate_alias(alias)
        if not self.path_for(alias).is_file():
            raise CredentialError(f"账户不存在，无法设为默认：{alias}")
        path = self.root / DEFAULT_ALIAS_FILE
        path.write_text(f"{alias}\n", encoding="utf-8")
        os.chmod(path, _FILE_MODE)

    # ---------- 内部 ----------

    def _read_envelope(self, path: Path) -> dict[str, Any]:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CredentialError(f"凭据文件无法读取：{path}（{exc.strerror}）") from exc
        try:
            envelope = json.loads(raw)
        except ValueError as exc:
            raise CredentialError(f"凭据文件损坏：{path} 不是合法 JSON") from exc
        if not isinstance(envelope, dict):
            raise CredentialError(f"凭据文件损坏：{path} 顶层不是对象")
        if envelope.get("v") != ENVELOPE_VERSION:
            raise CredentialError(
                f"凭据文件版本不支持：{path}（v={envelope.get('v')}，"
                f"当前支持 v={ENVELOPE_VERSION}）"
            )
        return envelope

    def _decrypt(self, envelope: dict[str, Any], alias: str, path: Path) -> bytes:
        try:
            nonce = base64.b64decode(envelope["nonce"], validate=True)
            ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise CredentialError(f"凭据文件损坏：{path} 缺少 nonce 或 ciphertext") from exc
        return crypto.open_bytes(
            self._aead, nonce, ciphertext, _aad(alias), label=f"凭据文件 {path}"
        )
