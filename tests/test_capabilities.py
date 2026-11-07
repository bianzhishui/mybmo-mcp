from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

CAPABILITIES = Path(__file__).parents[1] / "src" / "mybmo_mcp" / "baidu" / "capabilities.json"


@pytest.fixture(scope="module")
def data() -> dict[str, Any]:
    return json.loads(CAPABILITIES.read_text(encoding="utf-8"))


def collect_refs(node: Any, found: set[str]) -> set[str]:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/schemas/"):
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            collect_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            collect_refs(item, found)
    return found


def test_capability_count_matches_sdk_index(data: dict[str, Any]) -> None:
    assert len(data["capabilities"]) == 389
    assert data["meta"]["capabilities"] == 389


def test_every_capability_has_required_fields(data: dict[str, Any]) -> None:
    for key, cap in data["capabilities"].items():
        assert cap["endpoint"].startswith("/json/"), key
        assert cap["product_line"] in {"sms", "feed"}, key
        assert cap["mutating"] in {"true", "false", "unknown"}, key
        assert isinstance(cap["input_schema"], dict) and cap["input_schema"], key


def test_all_schema_refs_resolve(data: dict[str, Any]) -> None:
    schemas = data["schemas"]
    refs: set[str] = set()
    for cap in data["capabilities"].values():
        collect_refs(cap["input_schema"], refs)
    for schema in schemas.values():
        collect_refs(schema, refs)

    dangling = sorted(refs - set(schemas))
    assert dangling == [], f"悬空 $ref：{dangling}"


def test_nested_structure_is_preserved(data: dict[str, Any]) -> None:
    """请求体保持嵌套：campaignTypes[] 里的 campaignName 不能被展开成点路径。"""
    cap = data["capabilities"]["CampaignService.addCampaign"]
    assert cap["mutating"] == "true"

    body = data["schemas"][cap["body"]]
    assert set(body["properties"]) == {"campaignTypes", "adType"}

    ref = body["properties"]["campaignTypes"]["items"]["$ref"].rsplit("/", 1)[-1]
    campaign_type = data["schemas"][ref]
    assert "campaignName" in campaign_type["properties"]
    assert "budget" in campaign_type["properties"]


def test_method_name_comes_from_endpoint_path(data: dict[str, Any]) -> None:
    """SDK 的 operation_id 会把缩写弄坏（get_krby_query），必须以端点路径为准。"""
    cap = data["capabilities"]["KRService.getKRByQuery"]
    assert cap["endpoint"].endswith("/KRService/getKRByQuery")
    assert cap["mutating"] == "false"


def test_unknown_set_stays_small_and_reviewable(data: dict[str, Any]) -> None:
    unknown = [key for key, cap in data["capabilities"].items() if cap["mutating"] == "unknown"]
    assert len(unknown) <= 5, f"unknown 变多，需要复核判据：{unknown}"
