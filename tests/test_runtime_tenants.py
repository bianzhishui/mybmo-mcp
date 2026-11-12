"""运行时租户解析：client_id → 租户、上下文缓存、fail closed。

不需要数据库：映射表用替身，file 后端本来就是单租户。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mybmo_mcp.auth.tenancy import Principal, TenancyError
from mybmo_mcp.config import Settings
from mybmo_mcp.tools.runtime import Runtime

KEY_B64 = "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="  # base64(b"k" * 32)


def make_runtime(tmp_path: Path, **overrides: Any) -> Runtime:
    fields: dict[str, Any] = {
        "mybmo_data_dir": tmp_path,
        "mybmo_master_key": KEY_B64,
        "mybmo_auth_mode": "oidc",
    }
    fields.update(overrides)
    return Runtime(settings=Settings(**fields))


class FakeTenantMap:
    def __init__(self) -> None:
        self.resolved: list[str] = []

    def resolve(self, client_id: str) -> str:
        self.resolved.append(client_id)
        return f"tenant-of-{client_id}"


def test_file_backend_is_single_tenant(tmp_path: Path) -> None:
    """file 后端没有租户维度：一律用配置值（后续会整体移除）。"""
    runtime = make_runtime(tmp_path, mybmo_tenancy_backend="file", mybmo_tenant_id="t_only")

    assert runtime._tenant_for_client("whatever") == "t_only"


def test_pg_backend_resolves_through_map(tmp_path: Path) -> None:
    runtime = make_runtime(
        tmp_path,
        mybmo_tenancy_backend="pg",
        mybmo_database_url="postgresql://unused/unused",
    )
    fake = FakeTenantMap()
    runtime.__dict__["tenant_map"] = fake  # 绕过真实连接

    assert runtime._tenant_for_client("client-x") == "tenant-of-client-x"
    assert fake.resolved == ["client-x"]


def test_pg_backend_requires_client_id(tmp_path: Path) -> None:
    runtime = make_runtime(
        tmp_path,
        mybmo_tenancy_backend="pg",
        mybmo_database_url="postgresql://unused/unused",
    )

    with pytest.raises(TenancyError, match="缺少 client_id"):
        runtime._tenant_for_client("")


def test_context_is_cached_per_tenant(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)

    first = runtime.context_for_tenant("t_a")
    second = runtime.context_for_tenant("t_a")
    other = runtime.context_for_tenant("t_b")

    assert first is second
    assert first is not other
    assert first.tenant_id == "t_a"


def test_context_for_principal_uses_its_tenant(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    principal = Principal(sub="u_1", tenant_id="t_acme", source="oidc")

    ctx = runtime.context(principal)

    assert ctx.tenant_id == "t_acme"


def test_context_without_tenant_is_refused(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    principal = Principal(sub="u_1", tenant_id="", source="oidc")

    with pytest.raises(TenancyError, match="缺少租户"):
        runtime.context(principal)
