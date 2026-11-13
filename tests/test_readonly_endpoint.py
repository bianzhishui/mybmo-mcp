"""只读入口 `/mcp/readonly`。

对应需求单 `docs/req-mybmo-mcp-readonly-endpoint.md` §六 的 8 条验收用例，
外加两项结构保证：路径配置失败关闭、开关关闭时装配原样（ADR 0011）。

这里的断言都冲着"结构"而不是"提示词"：工具**不存在**、能力**调不动**、
拒绝**留了痕**。三件事任何一件松掉，写边界就退回成纪律。
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from tests.support.memory import MemoryAuditSink

from mybmo_mcp.auth.tenancy import Principal
from mybmo_mcp.baidu.capabilities import CapabilityRegistry
from mybmo_mcp.config import Settings
from mybmo_mcp.tools import accounts as account_tools
from mybmo_mcp.tools import capability as capability_tools
from mybmo_mcp.tools.runtime import Runtime, set_runtime

KEY_B64 = base64.b64encode(b"k" * 32).decode()

FULL_TOOLS = {
    "list_accounts",
    "auth_status",
    "bind_account",
    "share_account",
    "revoke_share",
    "list_shares",
    "query_audit",
    "search_capabilities",
    "get_capability_schema",
    "execute_capability",
}

READONLY_TOOLS = FULL_TOOLS - {"bind_account", "share_account", "revoke_share"}


# ---------- 测试替身 ----------


class RecordingMCP:
    """只记录注册了哪些工具，不实现协议——用来断言"注册期就少了几个"。"""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *_: Any, **__: Any):
        def wrap(fn: Any) -> Any:
            self.tools[fn.__name__] = fn
            return fn

        return wrap


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call(
        self,
        service: str,
        method: str,
        body: dict[str, Any] | None = None,
        *,
        product_line: str = "sms",
        dry_run: bool = False,
    ) -> Any:
        self.calls.append({"service": service, "method": method, "body": body})
        return {"requestId": "fake", "data": []}


class PermissiveAccess:
    def resolve(
        self, principal: Principal, requested: str | None = None, *, permission: str = "read"
    ) -> SimpleNamespace:
        return SimpleNamespace(alias=requested or "axd", user_name="示例账户")


class FakeRuntime:
    def __init__(self) -> None:
        self.capabilities = CapabilityRegistry.load()
        self.transport = FakeTransport()
        self.access = PermissiveAccess()
        self.audit = MemoryAuditSink()

    def current_principal(self) -> Principal:
        return Principal(sub="u_zhang", tenant_id="t_default", display_name="张三")

    def context(self, principal: Principal | None = None) -> SimpleNamespace:
        return SimpleNamespace(access=self.access, tenancy=None, app_secrets=None)

    def transport_for(self, alias: str | None = None, **_: Any) -> FakeTransport:
        return self.transport


@pytest.fixture
def runtime() -> Iterator[FakeRuntime]:
    fake = FakeRuntime()
    set_runtime(fake)  # type: ignore[arg-type]
    yield fake
    set_runtime(None)


@pytest.fixture
def readonly_execute(runtime: FakeRuntime):
    """取**注册期真正会被挂上去的**那个 execute_capability（只读版）。"""
    mcp = RecordingMCP()
    capability_tools.register(mcp, readonly=True)
    return mcp.tools["execute_capability"]


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        mybmo_data_dir=tmp_path / "data",
        mybmo_master_key=KEY_B64,
        casdoor_issuer="https://auth.example",
        mybmo_jwks_url="https://auth.example/.well-known/jwks",
        mybmo_resource_url="https://mcp.example",
        **overrides,
    )


# ---------- 用例 1 / 7：工具面 ----------


def test_readonly_endpoint_lists_exactly_seven_tools(tmp_path: Path) -> None:
    from mybmo_mcp.server import build_server

    set_runtime(Runtime(settings=_settings(tmp_path)))
    try:
        names = {tool.name for tool in build_server(readonly=True)._tool_manager.list_tools()}
    finally:
        set_runtime(None)

    assert names == READONLY_TOOLS
    assert len(names) == 7


def test_full_endpoint_still_registers_ten_tools(tmp_path: Path) -> None:
    """回归：默认入口一个都不能少（含三个写工具）。"""
    from mybmo_mcp.server import build_server

    set_runtime(Runtime(settings=_settings(tmp_path)))
    try:
        names = {tool.name for tool in build_server()._tool_manager.list_tools()}
    finally:
        set_runtime(None)

    assert names == FULL_TOOLS


def test_readonly_account_tools_skip_authorisation_writers() -> None:
    mcp = RecordingMCP()
    account_tools.register(mcp, readonly=True)

    assert set(mcp.tools) == {"list_accounts", "auth_status", "list_shares"}


# ---------- 用例 2 / 3：写能力被拒 ----------


@pytest.mark.parametrize(
    "capability",
    ["ImageManageService.uploadImage", "CampaignShoppingService.addCampaignShopping"],
)
async def test_readonly_refuses_write_capabilities(
    readonly_execute: Any, runtime: FakeRuntime, capability: str
) -> None:
    result = await readonly_execute(capability, input={})

    assert result["ok"] is False
    assert "readonly endpoint" in result["error"]["message"]
    assert "mutating=true" in result["error"]["message"]
    assert "/mcp" in result["error"]["message"]
    assert runtime.transport.calls == []  # 一个字节都没发出去


# ---------- 用例 5：unknown 按写处理 ----------


async def test_readonly_refuses_unknown_mutating_capability(
    readonly_execute: Any, runtime: FakeRuntime
) -> None:
    """全表只有这一条 ``mutating=unknown``，判不准时偏向更严的一侧。"""
    result = await readonly_execute("PreAuditService.materialPreAudit", input={})

    assert result["ok"] is False
    assert "mutating=unknown" in result["error"]["message"]
    assert runtime.transport.calls == []


# ---------- 用例 4：读能力放行 ----------


async def test_readonly_allows_read_capabilities(
    readonly_execute: Any, runtime: FakeRuntime
) -> None:
    result = await readonly_execute("KRService.getKRByQuery", input={})

    assert result["ok"] is True
    assert result["meta"]["mutating"] == "false"
    assert runtime.transport.calls[0]["service"] == "KRService"


# ---------- 拒绝要留痕 ----------


async def test_refusal_is_audited_as_denied(
    readonly_execute: Any, runtime: FakeRuntime
) -> None:
    await readonly_execute("ImageManageService.uploadImage", input={"imageUrl": "x"})

    rows = runtime.audit.rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["state"] == "denied"
    assert row["capability"] == "ImageManageService.uploadImage"
    assert row["mutating"] == "true"
    assert row["sub"] == "u_zhang"
    assert row["user_name"] == "张三"


# ---------- 用例 6：只读工具全部可用（注册期就在） ----------


def test_readonly_registers_every_read_tool() -> None:
    """除 execute_capability 外，其余只读工具都是纯读，直接由同一份实现挂上。"""
    mcp = RecordingMCP()
    capability_tools.register(mcp, readonly=True)

    assert set(mcp.tools) == {
        "search_capabilities",
        "get_capability_schema",
        "execute_capability",
    }
    # 是**同一份实现**，不是复制品：只读入口与全量入口除 execute 外行为不得漂移
    assert mcp.tools["search_capabilities"] is capability_tools.search_capabilities
    assert mcp.tools["get_capability_schema"] is capability_tools.get_capability_schema


def test_account_tools_are_shared_not_copied() -> None:
    mcp = RecordingMCP()
    account_tools.register(mcp, readonly=True)

    assert mcp.tools["list_accounts"] is account_tools.list_accounts
    assert mcp.tools["auth_status"] is account_tools.auth_status
    assert mcp.tools["list_shares"] is account_tools.list_shares


async def test_full_endpoint_still_executes_write_capabilities(runtime: FakeRuntime) -> None:
    """回归：守卫只存在于只读那份实现上，全量入口照旧忠实执行（ADR 003）。"""
    result = await capability_tools.execute_capability("ImageManageService.uploadImage", input={})

    assert result["ok"] is True
    assert runtime.transport.calls[0]["service"] == "ImageManageService"


# ---------- 用例 8 / 配置：开关与路径 ----------


def test_readonly_path_is_empty_when_switch_off(tmp_path: Path) -> None:
    from mybmo_mcp.server import readonly_path

    assert readonly_path(_settings(tmp_path)) == ""


def test_readonly_path_defaults_to_subpath(tmp_path: Path) -> None:
    from mybmo_mcp.server import readonly_path

    path = readonly_path(_settings(tmp_path, mybmo_mcp_readonly=True))

    assert path == "/mcp/readonly"


@pytest.mark.parametrize("bad", ["/mcp", "/mcp/", "mcp/readonly", "/"])
def test_readonly_path_rejects_configuration_that_shadows_mcp(
    tmp_path: Path, bad: str
) -> None:
    """配错就拒绝启动：让只读入口悄悄没挂上、或与全量入口打架，都比报错危险。"""
    from mybmo_mcp.server import readonly_path

    with pytest.raises(ValueError):
        readonly_path(_settings(tmp_path, mybmo_mcp_readonly=True, mybmo_mcp_readonly_path=bad))


# ---------- 装配：两条路由 ----------


def test_http_app_mounts_both_paths(tmp_path: Path) -> None:
    from mybmo_mcp.server import build_http_app

    set_runtime(Runtime(settings=_settings(tmp_path, mybmo_mcp_readonly=True)))
    try:
        app = build_http_app(with_auth=False, readonly="/mcp/readonly")
        paths = [getattr(route, "path", None) for route in app.routes]
    finally:
        set_runtime(None)

    assert "/mcp" in paths
    assert "/mcp/readonly" in paths
    # 只读入口不能再挂一份探活/回调/订阅：那是主机级路由
    assert paths.count("/healthz") == 1


def test_http_app_is_untouched_when_readonly_off(tmp_path: Path) -> None:
    from mybmo_mcp.server import build_http_app, build_server

    set_runtime(Runtime(settings=_settings(tmp_path)))
    try:
        composed = build_http_app(with_auth=False, readonly="")
        direct = build_server(with_auth=False).streamable_http_app()
        composed_paths = [getattr(r, "path", None) for r in composed.routes]
        direct_paths = [getattr(r, "path", None) for r in direct.routes]
    finally:
        set_runtime(None)

    assert composed_paths == direct_paths


# ---------- 端到端：两个入口都能谈判、工具面各是 10 / 7 ----------

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0"},
    },
}
_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


async def test_both_endpoints_serve_their_own_tool_list(tmp_path: Path) -> None:
    """唯一能证明"session manager 真的起来了"的用例。

    两个坑都在这里兜着：父应用 lifespan 没启动的话 ``initialize`` 会抛
    ``Task group is not initialized``；路由挂错的话拿到的是 404/307。
    ASGITransport 不跑 lifespan，所以手动 ``async with app.router.lifespan_context``。
    """
    from mybmo_mcp.server import build_http_app

    set_runtime(Runtime(settings=_settings(tmp_path, mybmo_mcp_readonly=True)))
    try:
        app = build_http_app(with_auth=False, readonly="/mcp/readonly")
        transport = httpx2.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            # 127.0.0.1 会开 DNS rebinding 保护，Host 必须带端口
            async with httpx2.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8000"
            ) as client:
                found: dict[str, set[str]] = {}
                for path in ("/mcp", "/mcp/readonly"):
                    init = await client.post(path, json=_INIT, headers=_HEADERS)
                    assert init.status_code == 200, init.text
                    session = init.headers.get("mcp-session-id")
                    assert session
                    listing = await client.post(
                        path,
                        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                        headers={
                            **_HEADERS,
                            "mcp-session-id": session,
                            "mcp-protocol-version": "2025-06-18",
                        },
                    )
                    assert listing.status_code == 200, listing.text
                    found[path] = set(re.findall(r'"name":"([a-z_]+)"', listing.text))
    finally:
        set_runtime(None)

    assert found["/mcp"] == FULL_TOOLS
    assert found["/mcp/readonly"] == READONLY_TOOLS


# ---------- 装配分支：开关关闭走原路径，打开才自己组 app ----------


def test_switch_off_keeps_using_mcpserver_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4 的结构保证：关闭时装配路径一行不换，不靠"测试碰巧过"。"""
    from mybmo_mcp import server as server_mod

    calls: list[dict[str, Any]] = []

    class FakeServer:
        def run(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(server_mod, "build_server", lambda **_: FakeServer())
    monkeypatch.setattr(server_mod, "build_http_app", lambda **_: pytest.fail("不该组装父 app"))

    set_runtime(Runtime(settings=_settings(tmp_path)))
    try:
        assert server_mod.main(["-t", "streamable-http", "-p", "9999"]) == 0
    finally:
        set_runtime(None)

    assert calls == [{"transport": "streamable-http", "host": "127.0.0.1", "port": 9999}]


def test_switch_on_serves_composed_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mybmo_mcp import server as server_mod

    seen: dict[str, Any] = {}

    def fake_build_http_app(**kwargs: Any) -> Any:
        seen["build"] = kwargs
        return object()

    def fake_uvicorn_run(app: Any, **kwargs: Any) -> None:
        seen["uvicorn"] = kwargs

    monkeypatch.setattr(server_mod, "build_http_app", fake_build_http_app)
    monkeypatch.setattr(server_mod.uvicorn, "run", fake_uvicorn_run)
    monkeypatch.setattr(
        server_mod, "build_server", lambda **_: pytest.fail("开关打开时不该走 MCPServer.run")
    )

    set_runtime(Runtime(settings=_settings(tmp_path, mybmo_mcp_readonly=True)))
    try:
        assert server_mod.main(["-t", "streamable-http", "-p", "9999"]) == 0
    finally:
        set_runtime(None)

    assert seen["build"] == {"readonly": "/mcp/readonly"}
    assert seen["uvicorn"]["port"] == 9999
