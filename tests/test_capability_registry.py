from __future__ import annotations

from mybmo_mcp.baidu.capabilities import CapabilityRegistry


def test_registry_stats() -> None:
    registry = CapabilityRegistry.load()

    assert registry.stats["capabilities"] == 396
    assert registry.stats["schemas"] == 595
    assert registry.stats["services"] == 131


def test_get_is_case_insensitive() -> None:
    registry = CapabilityRegistry.load()

    assert registry.get("KRService.getKRByQuery") is not None
    assert registry.get("krservice.getkrbyquery") is not None
    assert registry.get("NoSuchService.nope") is None


def test_chinese_alias_finds_keyword_planner() -> None:
    registry = CapabilityRegistry.load()

    hits = registry.search("拓词")

    assert hits, "中文别名检索失败"
    assert hits[0].key == "KRService.getKRByQuery"


def test_method_and_service_names_are_searchable() -> None:
    registry = CapabilityRegistry.load()

    assert any(cap.service == "CampaignService" for cap in registry.search("campaign"))
    assert registry.search("getAccountInfo")[0].key == "AccountService.getAccountInfo"


def test_search_returns_empty_for_nonsense_and_empty_query() -> None:
    registry = CapabilityRegistry.load()

    assert registry.search("完全不存在的词zzz") == []
    assert registry.search("   ") == []


def test_search_respects_limit() -> None:
    registry = CapabilityRegistry.load()

    assert len(registry.search("get", limit=3)) == 3


def test_schema_closure_only_includes_referenced_models() -> None:
    registry = CapabilityRegistry.load()
    cap = registry.get("CampaignService.addCampaign")
    assert cap is not None

    closure = registry.schema_closure(cap)

    assert "ApiCampaignAddRequest" in closure
    assert "ApiCampaignType" in closure
    assert len(closure) < len(registry._schemas)


def test_summary_is_non_empty_and_flags_mutation() -> None:
    registry = CapabilityRegistry.load()

    write = registry.get("CampaignService.addCampaign")
    read = registry.get("KRService.getKRByQuery")
    assert write is not None and read is not None

    # 摘要末尾可能跟实测标记（✅ / ◐），去掉标记再判读写标记
    assert write.summary and write.summary.removesuffix("｜✅").removesuffix("｜◐").endswith("｜写")
    assert read.summary and read.summary.removesuffix("｜✅").removesuffix("｜◐").endswith("｜读")
    assert write.is_mutating is True
    assert read.is_mutating is False
