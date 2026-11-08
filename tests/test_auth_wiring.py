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
    PERMISSION_ADMIN,
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
        # aud 是 client_id 而非资源地址，见 ADR 0006
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


def test_oidc_mode_without_audience_fails_loudly(tmp_path: Path) -> None:
    """audience 必须显式配 client_id。猜一个默认值比报错更危险。"""
    set_runtime(Runtime(settings=make_settings(tmp_path, mybmo_oidc_audience="")))

    with pytest.raises(TenancyError, match="缺少 MYBMO_OIDC_AUDIENCE"):
        build_server(with_auth=True)


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


def test_unknown_sub_gets_a_principal_with_no_grants(tmp_path: Path) -> None:
    """未登记的用户能"登录成功"，但看不到任何账户。

    这比直接报错好：用户看到的是"没有可用账户"，而不是"你的账号不存在"——
    后者会让人以为自己有账号只是服务坏了。
    """
    import json

    settings = make_settings(tmp_path)
    settings.mybmo_data_dir.mkdir(parents=True, exist_ok=True)
    (settings.mybmo_data_dir / "tenancy.json").write_text(
        json.dumps({"version": 1, "grants": []}), encoding="utf-8"
    )
    runtime = Runtime(settings=settings)

    principal = runtime.tenancy.resolve_principal("u_unknown")

    assert principal.sub == "u_unknown"
    assert principal.tenant_id == ""
    assert runtime.tenancy.permission_for(principal, "axd") is None


def test_identity_resolves_even_without_tenancy_file(tmp_path: Path) -> None:
    """授权表缺失时身份解析仍要成功——否则表现为 500 而不是"没有可用账户"。

    真正的拒绝发生在 require()/permission_for()，那里才抛 TenancyError。
    """
    runtime = Runtime(settings=make_settings(tmp_path))

    principal = runtime.tenancy.resolve_principal("u_zhang")

    assert principal.sub == "u_zhang"
    with pytest.raises(TenancyError, match="缺少授权表"):
        runtime.tenancy.permission_for(principal, "axd")


def test_known_sub_gets_tenant_and_display_name(tmp_path: Path) -> None:
    import json

    settings = make_settings(tmp_path)
    settings.mybmo_data_dir.mkdir(parents=True, exist_ok=True)
    (settings.mybmo_data_dir / "tenancy.json").write_text(
        json.dumps(
            {
                "version": 1,
                "users": [{"sub": "u_zhang", "tenant_id": "t_acme", "display_name": "张三"}],
                "grants": [{"sub": "u_zhang", "alias": "axd", "permission": "admin"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime = Runtime(settings=settings)

    principal = runtime.tenancy.resolve_principal("u_zhang")

    assert principal.tenant_id == "t_acme"
    assert principal.display_name == "张三"
    assert runtime.tenancy.permission_for(principal, "axd") == PERMISSION_ADMIN


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
