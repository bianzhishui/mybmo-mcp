"""凭据存储的抽象接口。

`FileVault`（加密文件）与 `PgCredentialStore`（PostgreSQL）实现同一套接口，
上层（`AccountRegistry` / `CredentialService` / `BindingManager`）只依赖这个协议，
不直接引用具体实现——ADR 0007 D3 的 ``MYBMO_CREDENTIAL_BACKEND=file|pg``。

接口刻意保持"窄"：只有按 alias 的增删查、枚举、默认账户。
跨账户的事务性操作（绑定落库、刷新加锁）由具体实现各自提供，
不在协议里假装统一——文件后端做不到行锁，硬统一只会变成两个都做不到。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from mybmo_mcp.auth.models import Credential


@runtime_checkable
class CredentialStore(Protocol):
    """一份凭据的存取。实现方负责加密与隔离。"""

    def save(self, credential: Credential) -> None:
        """写入或覆盖。``alias`` 是主键的一部分。"""
        ...

    def load(self, alias: str) -> Credential:
        """按 alias 读取。不存在或解密失败抛 :class:`CredentialError`。"""
        ...

    def delete(self, alias: str) -> None:
        """删除。不存在时静默成功（幂等）。"""
        ...

    def aliases(self) -> list[str]:
        """能正常读取的 alias 列表。"""
        ...

    def scan(self) -> tuple[list[str], list[tuple[Any, str]]]:
        """返回 ``(可用别名, [(位置, 原因)])``。

        损坏的条目不能静默消失——调用方要能决定是报错还是跳过。
        ``位置`` 在文件后端是 ``Path``，在 PG 后端是 ``credentials:<tenant>/<alias>``，
        调用方只把它当可打印的来源标识。
        """
        ...

    def default_alias(self) -> str | None:
        """默认账户（未显式指定 alias 时使用）。"""
        ...

    def set_default(self, alias: str) -> None:
        """设置默认账户。"""
        ...
