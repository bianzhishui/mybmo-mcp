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
