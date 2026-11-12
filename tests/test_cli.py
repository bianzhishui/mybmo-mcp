from __future__ import annotations

import pytest

from mybmo_mcp import server as server_mod


class FakeServer:
    """记录 run() 被调用的参数，避免真的监听端口。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def fake_server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    fake = FakeServer()
    monkeypatch.setattr(server_mod, "build_server", lambda: fake)
    return fake


def test_stdio_is_default(fake_server: FakeServer) -> None:
    assert server_mod.main([]) == 0
    assert fake_server.calls == [{"transport": "stdio"}]


def test_streamable_http_forwards_host_and_port(fake_server: FakeServer) -> None:
    argv = ["-t", "streamable-http", "--host", "10.0.0.5", "-p", "9999"]

    assert server_mod.main(argv) == 0
    assert fake_server.calls == [{"transport": "streamable-http", "host": "10.0.0.5", "port": 9999}]


def test_unknown_transport_is_rejected(fake_server: FakeServer) -> None:
    assert server_mod.main(["-t", "http"]) != 0
    assert fake_server.calls == []


def test_auth_bind_defaults_tenant_to_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """state 里的 tenant 是唯一能证明"这次绑定属于哪个租户"的字段，不能默认留空。"""
    import time
    from types import SimpleNamespace

    from typer.testing import CliRunner

    captured: dict[str, object] = {}

    class FakeManager:
        def authorize_url(self, **kwargs: object):
            captured.update(kwargs)
            state = SimpleNamespace(
                nonce="n",
                alias="main",
                subject="sub-1",
                tenant=kwargs["tenant"],
                app_id=kwargs["app_id"],
                expires_at=time.time() + 900,
            )
            return "https://example.invalid/auth", state

    class FakeRuntime:
        settings = SimpleNamespace(
            mybmo_auth_mode="oidc",
            mybmo_baidu_callback_url="https://example.invalid/cb",
            mybmo_tenant_id="t_default",
        )
        binding = FakeManager()

    monkeypatch.setattr(server_mod, "get_runtime", lambda: FakeRuntime())

    result = CliRunner().invoke(
        server_mod.app,
        [
            "auth",
            "bind",
            "--alias",
            "main",
            "--app-id",
            "20426c0457c842036585e268d456fe45",
            "--subject",
            "sub-1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["tenant"] == "t_default"
