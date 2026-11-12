"""凭据加解密的唯一入口：AES-GCM + AAD 域分离。

**为什么单独一层**：凭据（`credentials`）与应用密钥（`app_secrets`）在「加密文件」和
「PostgreSQL」两个后端上原本各有一套实现，同样的信封逻辑（主密钥校验、12 字节 nonce、
AAD 拼接、`InvalidTag` 转中文错误、JSON 序列化、原子写）重复了四份——
`vault.py` / `FileAppSecretStore` / `PgAppSecretStore` / `PgCredentialStore`。
收敛到这里以后，改加密策略只动一处。

**域分离靠 AAD 前缀**，把密文搬到别的标识下必然解不开：

| 前缀 | 用途 |
|---|---|
| `mybmo-mcp/credential/v1/<alias>` | 文件后端凭据（不带租户；存量就是它，不能动） |
| `mybmo-mcp/credential/v2/<tenant>/<alias>` | PG 后端凭据（带租户，挡跨租户搬行） |
| `mybmo-mcp/app/v1/<app_id>` | 应用密钥（两个后端共用） |
"""

from __future__ import annotations

import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mybmo_mcp.baidu.errors import CredentialError

MASTER_KEY_BYTES = 32
NONCE_BYTES = 12
DIR_MODE = 0o700


def aead(master_key: bytes) -> AESGCM:
    """构造 AEAD。密钥长度不对立刻失败——带着坏密钥跑起来，第一次写才会炸。"""
    if len(master_key) != MASTER_KEY_BYTES:
        raise CredentialError(
            f"主密钥必须是 {MASTER_KEY_BYTES} 字节，当前 {len(master_key)} 字节"
        )
    return AESGCM(master_key)


def aad(prefix: str, *parts: str) -> bytes:
    """拼装附加认证数据。空片段也要占位，避免 ``a/b`` 与 ``a//b`` 撞车。"""
    joined = "/".join(parts)
    return f"{prefix}{joined}".encode()


def seal(aead_obj: AESGCM, payload: dict[str, Any], aad_bytes: bytes) -> tuple[bytes, bytes]:
    """加密一个 JSON 对象，返回 ``(nonce, ciphertext)``。"""
    plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    nonce = os.urandom(NONCE_BYTES)
    return nonce, aead_obj.encrypt(nonce, plaintext, aad_bytes)


def open_bytes(
    aead_obj: AESGCM,
    nonce: bytes,
    cipher: bytes,
    aad_bytes: bytes,
    *,
    label: str,
) -> bytes:
    """解密。失败就报"解密失败 + 位置"，绝不静默当成"没有这条记录"。"""
    try:
        return aead_obj.decrypt(nonce, cipher, aad_bytes)
    except InvalidTag as exc:
        raise CredentialError(
            f"解密失败：{label}。可能原因：主密钥与加密时不一致、密文被篡改，"
            "或密文被搬到别的账户/租户/应用名下"
        ) from exc


def open_json(
    aead_obj: AESGCM,
    nonce: bytes,
    cipher: bytes,
    aad_bytes: bytes,
    *,
    label: str,
) -> dict[str, Any]:
    """解密并解析成对象。"""
    plaintext = open_bytes(aead_obj, nonce, cipher, aad_bytes, label=label)
    try:
        data = json.loads(plaintext)
    except ValueError as exc:
        raise CredentialError(f"密文损坏：{label} 解密后不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise CredentialError(f"密文损坏：{label} 解密后不是对象")
    return data

