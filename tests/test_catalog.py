from __future__ import annotations

from mybmo_mcp.baidu.catalog import Catalog


def test_catalog_matches_documented_size() -> None:
    catalog = Catalog.load()

    assert catalog.stats == {"services": 133, "methods": 399}


def test_describe_distinguishes_read_and_write() -> None:
    catalog = Catalog.load()

    read = catalog.describe("krService", "get_kr_by_query")
    assert read is not None
    assert read["method"] == "getKRByQuery"
    assert read["write"] is False
    assert read["product_line"] == "sms"

    write = catalog.describe("KeywordService", "addWord")
    assert write is not None
    assert write["write"] is True


def test_feed_service_reports_feed_product_line() -> None:
    catalog = Catalog.load()

    info = catalog.describe("campaignfeedService", "get_campaign_feed")
    assert info is not None
    assert info["product_line"] == "feed"


def test_unknown_service_returns_none() -> None:
    catalog = Catalog.load()

    assert catalog.describe("NoSuchService", "getWhatever") is None
    assert catalog.get("NoSuchService") is None


def test_search_finds_by_service_or_method() -> None:
    catalog = Catalog.load()

    by_service = catalog.search("krService")
    assert by_service and all(hit["service"] == "KRService" for hit in by_service)

    by_method = catalog.search("getbalance")
    assert any(hit["method"] == "getBalanceInfo" for hit in by_method)


def test_search_respects_limit() -> None:
    catalog = Catalog.load()

    assert len(catalog.search("get", limit=5)) == 5
    assert catalog.search("") == []
