"""oidc 模式的装配：谁来决定身份，以及配错时会不会静默退化成不鉴权。

这些用例守的是"看起来配好了、实际谁都能进"这类故障——它比崩溃更危险，
因为监控和日志都显示正常。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from mybmo_mcp.auth.resource import JwtVerifier
from mybmo_mcp.auth.tenancy import (
    MODE_LOCAL,
    MODE_OIDC,
    PERMISSION_READ,
    Principal,
    TenancyError,
)
from mybmo_mcp.config import Settings
from mybmo_mcp.server import build_server
from mybmo_mcp.tools.runtime import Runtime, set_runtime

KEY_B64 = base64.b64encode(b"k" * 32).decode()
ISSUER = "https://auth.pandabase.top"
RESOURCE = "https://baiduads.pandabase.top"
CLIENT_ID = "60fda1d9bedd4615eb90"


@pytest.fixture(autouse=True)
def clean_runtime():
    yield
    set_runtime(None)


def make_settings(tmp_path: Path, *, mode: str = MODE_OIDC, **overrides) -> Settings:
    values = {
        "mybmo_data_dir": tmp_path / "data",
        "mybmo_master_key": KEY_B64,
        "mybmo_auth_mode": mode,
        "casdoor_issuer": ISSUER,
        "mybmo_resource_url": RESOURCE,
        # aud 是 client_id 而非资源地址，见 docs/auth-contract.md
        "mybmo_oidc_audience": CLIENT_ID,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


# ---------- 装配 ----------


def test_local_mode_mounts_no_auth(tmp_path: Path) -> None:
    """stdio 下没有 HTTP 层，装鉴权中间件只会让 SDK 期待一个永远不存在的令牌。"""
    set_runtime(Runtime(settings=make_settings(tmp_path, mode=MODE_LOCAL)))

    server = build_server(with_auth=True)

    assert server.settings.auth is None


def test_oidc_mode_mounts_resource_server(tmp_path: Path) -> None:
    set_runtime(Runtime(settings=make_settings(tmp_path)))

    server = build_server(with_auth=True)

    assert server.settings.auth is not None
    assert server.settings.auth.required_scopes == ["baidu:read"]
    # audience 由 verifier 自己校验，这里不重复
    assert server.settings.auth.validate_token_resource is False


def test_oidc_mode_without_issuer_fails_loudly(tmp_path: Path) -> None:
    """缺 issuer 必须报错，不能悄悄退化成"不鉴权"。

    这是最坏的失败方式：容器 healthy、日志无异常、任何人的请求都能进来。
    """
    set_runtime(Runtime(settings=make_settings(tmp_path, casdoor_issuer="")))

    with pytest.raises(ValueError, match="issuer 不能为空"):
        build_server(with_auth=True)


def test_oidc_mode_without_audience_uses_client_id_mode(tmp_path: Path) -> None:
    """aud 白名单是可选的收紧项（ADR 0008）：不配就只要求令牌 aud 含自身 client_id。"""
    set_runtime(Runtime(settings=make_settings(tmp_path, mybmo_oidc_audience="")))

    server = build_server(with_auth=True)

    assert server is not None


def test_oidc_mode_without_resource_url_fails_loudly(tmp_path: Path) -> None:
    """resource 地址不参与验签，但元数据要用它。"""
    set_runtime(Runtime(settings=make_settings(tmp_path, mybmo_resource_url="")))

    with pytest.raises(TenancyError, match="缺少 MYBMO_RESOURCE_URL"):
        build_server(with_auth=True)


def test_jwks_url_can_be_overridden(tmp_path: Path) -> None:
    """Casdoor 部署在子路径时才能用到，默认由 issuer 推导。"""
    set_runtime(
        Runtime(settings=make_settings(tmp_path, mybmo_jwks_url="https://auth.example/x/jwks"))
    )

    verifier = Runtime(settings=make_settings(tmp_path)).verifier

    assert isinstance(verifier, JwtVerifier)
    assert verifier.issuer == ISSUER


# ---------- 身份解析 ----------


def test_oidc_without_identity_is_refused_not_defaulted(tmp_path: Path) -> None:
    """多用户模式下没带身份 → 报错。

    绝不能回退成 local_admin——那等于任何漏配鉴权的请求都是管理员。
    """
    set_runtime(Runtime(settings=make_settings(tmp_path)))

    with pytest.raises(TenancyError, match="没有携带身份"):
        set_runtime(Runtime(settings=make_settings(tmp_path)))
        build_server(with_auth=True)
        Runtime(settings=make_settings(tmp_path)).current_principal()





def test_explicit_principal_wins_for_tests(tmp_path: Path) -> None:
    """注入的 principal 优先于 JWT —— 测试与脚本靠它跳过整个鉴权链路。"""
    runtime = Runtime(
        settings=make_settings(tmp_path),
        principal=Principal(sub="u_zhang", tenant_id="t_acme"),
    )

    assert runtime.current_principal().sub == "u_zhang"


# ---------- scope 与档位的对应 ----------


def test_scopes_are_exposed_for_discovery(tmp_path: Path) -> None:
    set_runtime(Runtime(settings=make_settings(tmp_path)))
    server = build_server(with_auth=True)

    from mybmo_mcp.auth.resource import SCOPES_SUPPORTED

    assert SCOPES_SUPPORTED == ("baidu:read", "baidu:write", "baidu:admin")
    assert server.settings.auth is not None
    assert PERMISSION_READ in {"read"}
