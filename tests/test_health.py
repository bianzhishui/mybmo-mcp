"""探活端点。

三条断言对应三件部署时真正关心的事：进程活着、能力表加载得出来、凭据有没有配。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from mybmo_mcp.config import Settings
from mybmo_mcp.health import health_payload
from mybmo_mcp.tools.runtime import Runtime, set_runtime

KEY_B64 = base64.b64encode(b"k" * 32).decode()


def make_settings(tmp_path: Path, *, with_key: bool) -> Settings:
    return Settings(
        _env_file=None,
        mybmo_data_dir=tmp_path / "data",
        mybmo_master_key=KEY_B64 if with_key else "",
        mybmo_auth_mode="local",
    )


@pytest.fixture(autouse=True)
def clean_runtime():
    yield
    set_runtime(None)


def test_reports_ok_with_capabilities(tmp_path: Path) -> None:
    set_runtime(Runtime(settings=make_settings(tmp_path, with_key=True)))

    payload = health_payload()

    assert payload["status"] == "ok"
    assert payload["capabilities"] == 397
    assert payload["described"] >= 50
    assert payload["auth_mode"] == "local"
    assert payload["credentials"] == "configured"


def test_missing_master_key_is_reported_not_fatal(tmp_path: Path) -> None:
    """没有主密钥时服务仍然活着——能检索接口目录，只是调不动百度。

    这里必须是 200 + 字段告警，不能抛异常：否则容器被判死重启，
    而重启一百次也不会凭空长出密钥。
    """
    set_runtime(Runtime(settings=make_settings(tmp_path, with_key=False)))

    payload = health_payload()

    assert payload["status"] == "ok"
    assert payload["credentials"] == "unconfigured"
    assert payload["capabilities"] == 397


def test_health_does_not_leak_account_or_token_info(tmp_path: Path) -> None:
    set_runtime(Runtime(settings=make_settings(tmp_path, with_key=True)))

    keys = set(health_payload())

    assert keys == {
        "status",
        "version",
        "auth_mode",
        "capabilities",
        "described",
        "verified",
        "credentials",
    }


def test_route_is_mounted_on_the_http_app() -> None:
    from mybmo_mcp.server import build_server

    app = build_server().streamable_http_app()
    paths = {getattr(route, "path", None) for route in app.routes}

    assert "/healthz" in paths
