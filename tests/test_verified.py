"""``verified`` 这个维度：只做信息标记，不影响可调用性。"""

from __future__ import annotations

from typing import Any

import pytest

from mybmo_mcp.baidu.capabilities import VERIFIED_LEVELS, CapabilityRegistry, Verified


def test_verified_levels_are_documented_values() -> None:
    assert VERIFIED_LEVELS == ("read", "write", "partial")


@pytest.mark.parametrize(
    "raw",
    [
        {"at": "2026-11-11", "level": "read"},
        {"at": "2026-11-11", "level": "write", "basis": "b"},
        {"at": "2026-11-11", "level": "partial"},
    ],
)
def test_from_dict_accepts_valid_shapes(raw: dict[str, Any]) -> None:
    assert Verified.from_dict(raw) is not None


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"level": "read"},
        {"at": "2026-11-11"},
        {"at": "2026-11-11", "level": "verified"},
        {"at": "", "level": "read"},
        {"at": "2026-11-11", "level": "READ "},  # 去空格后小写，合法
    ],
)
def test_from_dict_rejects_bad_shapes(raw: dict[str, Any]) -> None:
    if raw.get("level", "").strip().lower() in VERIFIED_LEVELS and raw.get("at"):
        assert Verified.from_dict(raw) is not None
    else:
        assert Verified.from_dict(raw) is None


def test_marker_distinguishes_partial() -> None:
    assert Verified(at="2026-11-11", level="read").marker == "✅"
    assert Verified(at="2026-11-11", level="write").marker == "✅"
    assert Verified(at="2026-11-11", level="partial").marker == "◐"


def test_registry_exposes_verified_from_descriptions() -> None:
    registry = CapabilityRegistry.load()
    cap = registry.get("CampaignShoppingService.updateCampaignShopping")

    assert cap.verified is not None
    assert cap.verified.level == "write"
    assert cap.verified.at == "2026-11-11"
    assert "2079251858" in cap.verified.basis
    assert "✅" in cap.summary
    assert cap.to_summary()["verified"]["level"] == "write"


def test_unverified_capability_has_no_marker() -> None:
    registry = CapabilityRegistry.load()
    cap = registry.get("AccountService.updateAccountInfo")

    assert cap.verified is None
    assert cap.to_summary()["verified"] is None
    assert "✅" not in cap.summary


def test_stats_counts_verified() -> None:
    stats = CapabilityRegistry.load().stats

    assert stats["verified"] == 35
    assert stats["verified"] <= stats["capabilities"]


def test_invalid_verified_entry_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """写错档位要直接报错，不能静默当成"未验证"——那会让标记形同虚设。"""
    import json

    from mybmo_mcp.baidu import capabilities as mod

    raw = {
        "schemas": {},
        "capabilities": {
            "A.b": {
                "service": "A",
                "method": "b",
                "endpoint": "/x",
                "product_line": "sms",
                "mutating": "false",
            }
        },
    }
    descriptions = {
        "version": mod.DESCRIPTIONS_VERSION,
        "capabilities": {"A.b": {"description": "x", "verified": {"at": "", "level": "nope"}}},
    }

    with pytest.raises(ValueError, match="verified 不合法"):
        mod.CapabilityRegistry.from_dict(json.loads(json.dumps(raw)), descriptions=descriptions)
