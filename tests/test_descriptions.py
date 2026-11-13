"""手写描述：加载、注入、检索效果。

这些用例守的不是"文件存在"，而是三件会静默退化的事：

1. 描述文件引用了不存在的能力（改名或拼错）——加载时就该炸，不是等检索搜不到
2. 别名 key 对不上（``Openapi`` vs ``OpenApi``）——曾经真的让「报表」整组搜不到
3. 参数说明没写进 schema——写了等于没写，agent 看不到
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mybmo_mcp.baidu.capabilities import (
    DESCRIPTIONS_FILENAME,
    CapabilityRegistry,
)

PACKAGE = Path(__file__).parents[1] / "src" / "mybmo_mcp" / "baidu"
CAPABILITIES = PACKAGE / "capabilities.json"
DESCRIPTIONS = PACKAGE / DESCRIPTIONS_FILENAME


@pytest.fixture(scope="module")
def registry() -> CapabilityRegistry:
    return CapabilityRegistry.load()


@pytest.fixture(scope="module")
def raw_capabilities() -> dict[str, Any]:
    return json.loads(CAPABILITIES.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def raw_descriptions() -> dict[str, Any]:
    return json.loads(DESCRIPTIONS.read_text(encoding="utf-8"))


# ---------- 文件本身 ----------


def test_descriptions_file_is_shipped(raw_descriptions: dict[str, Any]) -> None:
    assert raw_descriptions["version"] == 1
    assert len(raw_descriptions["capabilities"]) >= 50


def test_every_described_capability_actually_exists(
    raw_capabilities: dict[str, Any], raw_descriptions: dict[str, Any]
) -> None:
    """引用不存在的能力说明改名或拼错了，必须现在发现。"""
    unknown = sorted(set(raw_descriptions["capabilities"]) - set(raw_capabilities["capabilities"]))
    assert unknown == []


def test_every_entry_has_a_description(raw_descriptions: dict[str, Any]) -> None:
    empty = [
        key
        for key, entry in raw_descriptions["capabilities"].items()
        if not str(entry.get("description") or "").strip()
    ]
    assert empty == [], f"这些条目没有 description：{empty}"


def test_no_dangling_aliases(registry: CapabilityRegistry) -> None:
    """别名写错不会报错，只会让检索悄悄失效——这条用例是唯一防线。"""
    assert registry.validate_aliases() == []


# ---------- 合并结果 ----------


def test_description_becomes_the_summary(registry: CapabilityRegistry) -> None:
    cap = registry.get("CampaignService.addCampaign")

    assert cap is not None
    assert cap.summary == "新建推广计划（不含单元和关键词）｜写"
    assert cap.notes


def test_undescribed_capability_falls_back_to_group(registry: CapabilityRegistry) -> None:
    cap = next(c for c in registry._capabilities.values() if not c.description)
    assert "｜" in cap.summary


def test_nested_param_description_is_injected(registry: CapabilityRegistry) -> None:
    """说明要落在模型定义上，这样 input_schema 与 schemas 两处都能读到。"""
    cap = registry.get("CampaignService.addCampaign")
    closure = registry.schema_closure(cap)
    campaign_type = closure["ApiCampaignType"]

    assert "元" in campaign_type["properties"]["budget"]["description"]
    assert "必填" not in campaign_type["properties"]["campaignName"].get("description", "")


def test_top_level_param_description_is_injected(registry: CapabilityRegistry) -> None:
    cap = registry.get("OpenApiReportService.getReportData")
    schema = registry.resolved_input_schema(cap)

    assert "起始行" in schema["properties"]["startRow"]["description"]
    assert "列元数据" in schema["properties"]["withColumnMeta"]["description"]


def test_shared_model_wording_stays_context_neutral(registry: CapabilityRegistry) -> None:
    """``ApiKeywordType`` 同时用于 addWord 与 updateWord。

    说明写在共享模型上，会同时出现在两个能力的 schema 里。所以不能写成
    「修改时必填」——那对 addWord 是错的，模型会以为新增也要传 keywordId。
    """
    add = registry.schema_closure(registry.get("KeywordService.addWord"))["ApiKeywordType"]
    update = registry.schema_closure(registry.get("KeywordService.updateWord"))["ApiKeywordType"]

    keyword_id = add["properties"]["keywordId"]["description"]
    assert "新增" in keyword_id and "修改" in keyword_id
    assert update["properties"]["keywordId"]["description"] == keyword_id
    assert "新增" in add["properties"]["adgroupId"]["description"]


def test_param_description_on_ref_target_does_not_pollute_model(
    registry: CapabilityRegistry,
) -> None:
    """``material.userPortrait`` 在自己的模型里只是一个 ``$ref: Picture``。

    说明必须写在那个 ``$ref`` 节点上。落到被引用的 ``Picture`` 定义里，就会把
    ``pictures`` / ``posters`` / ``broadcastCovers`` 全说成「品牌头像」。
    """
    cap = registry.get("CampaignShoppingService.addCampaignShopping")
    closure = registry.schema_closure(cap)
    material = closure["Material"]

    assert "品牌头像" in material["properties"]["userPortrait"]["description"]
    assert "description" not in closure["Picture"]
    assert "description" not in material["properties"]["pictures"]
    assert "description" not in material["properties"]["pictures"]["items"]


def test_described_count_matches_file(
    registry: CapabilityRegistry, raw_descriptions: dict[str, Any]
) -> None:
    assert registry.stats["described"] == len(raw_descriptions["capabilities"])


# ---------- 故障要吵，不要静默 ----------


def _slim_capabilities() -> dict[str, Any]:
    """只留两条能力，让「引用不存在的能力」这类用例跑得快、看得清。"""
    data = json.loads(CAPABILITIES.read_text(encoding="utf-8"))
    keep = {"CampaignService.addCampaign", "KRService.getKRByQuery"}
    data["capabilities"] = {k: v for k, v in data["capabilities"].items() if k in keep}
    return data


def test_unknown_capability_key_raises() -> None:
    with pytest.raises(ValueError, match="引用了不存在的能力"):
        CapabilityRegistry.from_dict(
            _slim_capabilities(),
            descriptions={"version": 1, "capabilities": {"CampaignService.addCampagn": {}}},
        )


def test_unknown_param_path_raises() -> None:
    descriptions = {
        "version": 1,
        "capabilities": {"CampaignService.addCampaign": {"params": {"campaignTypes[].nope": "x"}}},
    }

    with pytest.raises(ValueError, match="不存在的参数"):
        CapabilityRegistry.from_dict(_slim_capabilities(), descriptions=descriptions)


def test_unsupported_version_raises() -> None:
    with pytest.raises(ValueError, match="版本"):
        CapabilityRegistry.from_dict(
            _slim_capabilities(), descriptions={"version": 99, "capabilities": {}}
        )


# ---------- 检索效果 ----------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # 同分时只读优先：查报表 -> 同步取数；导出才 -> 建任务
        ("报表", "OpenApiReportService.getReportData"),
        ("导出报表", "OpenApiReportService.createReportTask"),
        ("拉报表", "OpenApiReportService.getReportData"),
        ("拓词", "KRService.getKRByQuery"),
        ("余额", "BalanceService.getBalanceInfo"),
        ("加词", "KeywordService.addWord"),
        ("删计划", "CampaignService.deleteCampaign"),
        ("操作日志", "ToolkitService.getOperationRecord"),
    ],
)
def test_chinese_query_hits_expected_top1(
    registry: CapabilityRegistry, query: str, expected: str
) -> None:
    found = registry.search(query, limit=3)

    assert found, f"「{query}」搜不到任何能力"
    assert found[0].key == expected, [cap.key for cap in found]


def test_report_service_is_reachable_at_all(registry: CapabilityRegistry) -> None:
    """曾经的 bug：别名表写成 OpenapiReportService，与实际 OpenApi 大小写不符，整组失效。"""
    keys = {cap.key for cap in registry.search("报表", limit=10)}

    assert any(key.startswith("OpenApiReportService.") for key in keys)
