from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from mybmo_mcp.audit.log import AuditLog
from mybmo_mcp.auth.tenancy import Principal
from mybmo_mcp.baidu.capabilities import CapabilityRegistry
from mybmo_mcp.tools import capability as capability_tools
from mybmo_mcp.tools.runtime import set_runtime


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
        self.calls.append(
            {"service": service, "method": method, "body": body, "product_line": product_line}
        )
        return {"requestId": "fake", "data": []}


class PermissiveAccess:
    """放行一切的授权层。真实的越权判定在 test_tenancy.py 里测。"""

    def resolve(
        self, principal: Principal, requested: str | None = None, *, permission: str = "read"
    ) -> SimpleNamespace:
        return SimpleNamespace(alias=requested or "axd")


class FakeRuntime:
    def __init__(self) -> None:
        self.capabilities = CapabilityRegistry.load()
        self.transport = FakeTransport()
        self.permissions: list[str] = []
        self.access = PermissiveAccess()
        self.audit = AuditLog(None)
        self.principal: Principal | None = None

    def current_principal(self) -> Principal:
        return self.principal or Principal.local_admin()

    def context(self, principal: Principal | None = None):
        target = principal or self.current_principal()
        return SimpleNamespace(
            tenant_id=target.tenant_id or "local",
            store=None,
            tenancy=None,
            app_secrets=None,
            registry=None,
            access=self.access,
            credentials=None,
        )

    def transport_for(
        self, alias: str | None = None, *, permission: str = "read", **_: Any
    ) -> FakeTransport:
        self.permissions.append(permission)
        return self.transport


@pytest.fixture
def runtime() -> Iterator[FakeRuntime]:
    fake = FakeRuntime()
    set_runtime(fake)  # type: ignore[arg-type]
    yield fake
    set_runtime(None)


async def test_search_tool_returns_summaries_without_schemas(runtime: FakeRuntime) -> None:
    result = await capability_tools.search_capabilities("拓词")

    assert result["ok"] is True
    assert result["data"]["count"] >= 1
    first = result["data"]["capabilities"][0]
    assert first["capability"] == "KRService.getKRByQuery"
    assert "input_schema" not in first


async def test_schema_tool_returns_closure(runtime: FakeRuntime) -> None:
    result = await capability_tools.get_capability_schema("CampaignService.addCampaign")

    assert result["ok"] is True
    data = result["data"]
    assert data["endpoint"].endswith("/CampaignService/addCampaign")
    assert "ApiCampaignType" in data["schemas"]
    assert data["notes"]


async def test_schema_tool_resolves_top_level_ref(runtime: FakeRuntime) -> None:
    """产物里 input_schema 是 $ref，工具必须解开，否则 agent 看不到字段。"""
    result = await capability_tools.get_capability_schema("KeywordService.addWord")

    assert result["ok"] is True
    properties = result["data"]["input_schema"]["properties"]
    assert "keywordTypes" in properties


async def test_schema_tool_unknown_capability_suggests(runtime: FakeRuntime) -> None:
    result = await capability_tools.get_capability_schema("CampaignService.addCampaig")

    assert result["ok"] is False
    assert "可能" in result["error"]["hint"]


async def test_execute_normalizes_snake_case_to_camel(runtime: FakeRuntime) -> None:
    result = await capability_tools.execute_capability(
        "CampaignService.addCampaign",
        input={"campaign_types": [{"campaign_name": "测试计划", "budget": 100}]},
    )

    assert result["ok"] is True
    body = runtime.transport.calls[0]["body"]
    assert body == {"campaignTypes": [{"campaignName": "测试计划", "budget": 100}]}
    assert result["meta"]["normalized"]


async def test_execute_rejects_unknown_field_with_suggestion(runtime: FakeRuntime) -> None:
    result = await capability_tools.execute_capability(
        "KeywordService.addWord", input={"adgroup_id": 1}
    )

    assert result["ok"] is False
    assert "不是该能力的参数" in result["error"]["message"]
    assert runtime.transport.calls == []


async def test_execute_passes_product_line(runtime: FakeRuntime) -> None:
    await capability_tools.execute_capability("CampaignFeedService.getCampaignFeed", input={})

    assert runtime.transport.calls[0]["product_line"] == "feed"


async def test_execute_wait_is_reported_as_ignored(runtime: FakeRuntime) -> None:
    result = await capability_tools.execute_capability(
        "KRService.getKRByQuery", input={"query": "月季"}, wait=True
    )

    assert result["ok"] is True
    assert "ignored" in result["meta"]["wait"]


async def test_execute_unknown_capability(runtime: FakeRuntime) -> None:
    result = await capability_tools.execute_capability("Nope.nothing", input={})

    assert result["ok"] is False
    assert "没有这个能力" in result["error"]["message"]


async def test_execute_requires_write_permission_for_mutating(runtime: FakeRuntime) -> None:
    """写能力要 write 档位。判定依据是能力的 mutating，不是方法名前缀。"""
    result = await capability_tools.execute_capability(
        "CampaignService.addCampaign", input={"campaignTypes": []}
    )

    assert result["ok"] is True
    assert runtime.permissions == ["write"]
    assert result["meta"]["required_permission"] == "write"


async def test_execute_requires_only_read_permission_for_query(runtime: FakeRuntime) -> None:
    result = await capability_tools.execute_capability(
        "KRService.getKRByQuery", input={"query": "月季"}
    )

    assert result["ok"] is True
    assert runtime.permissions == ["read"]
    assert result["meta"]["required_permission"] == "read"
