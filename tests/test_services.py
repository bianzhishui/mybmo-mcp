from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest

from mybmo_mcp.baidu.services import accounts, keyword
from mybmo_mcp.baidu.transport import BaiduTransport, RequestCredentials, StaticCredentials

FIXTURES = Path(__file__).parent / "fixtures"

BALANCE_OK = json.dumps(
    {"header": {"status": 0}, "body": {"data": [{"balance": 12500.5, "budget": 800.0}]}}
)
BALANCE_DENIED = json.dumps(
    {
        "header": {
            "status": 4,
            "failures": [{"code": 89406, "message": "The access token you provided is invalidate"}],
        },
        "body": {},
    }
)
EMPTY_DATA = json.dumps({"header": {"status": 0}, "body": {"data": []}})


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def transport(routes: dict[str, str]) -> BaiduTransport:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        for suffix, body in routes.items():
            if url.endswith(suffix):
                return httpx2.Response(200, text=body)
        raise AssertionError(f"未预期的请求：{url}")

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    creds = StaticCredentials(RequestCredentials(user_name="示例账户", access_token="tok"))
    return BaiduTransport(creds, client=client, min_interval=0, backoff_base=0)


async def test_snapshot_summarizes_account_and_balance() -> None:
    client = transport(
        {
            "AccountService/getAccountInfo": fixture_text("account_info.json"),
            "BalanceService/getBalanceInfo": BALANCE_OK,
        }
    )

    result = await accounts.snapshot(client)

    assert result["summary"]["userName"] == "示例科技（已脱敏）"
    assert result["summary"]["isActive"] is True
    assert result["extra"] == {}
    assert result["balance"][0]["balance"] == 12500.5
    assert "balance_error" not in result


async def test_snapshot_reports_balance_permission_gap_instead_of_failing() -> None:
    client = transport(
        {
            "AccountService/getAccountInfo": fixture_text("account_info.json"),
            "BalanceService/getBalanceInfo": BALANCE_DENIED,
        }
    )

    result = await accounts.snapshot(client)

    assert result["summary"]["userName"] == "示例科技（已脱敏）"
    assert result["balance"] is None
    assert result["balance_error"]["code"] == 89406
    assert "权限" in result["balance_error"]["hint"]


async def test_snapshot_marks_empty_response() -> None:
    client = transport({"AccountService/getAccountInfo": EMPTY_DATA})

    result = await accounts.snapshot(client, include_balance=False)

    assert result["summary"] == {}
    assert "note" in result
    assert "balance" not in result


async def test_expand_normalizes_and_sorts_by_pv() -> None:
    client = transport({"KRService/getKRByQuery": fixture_text("kr_expand.json")})

    rows = await keyword.expand(client, "月季")

    assert [row["word"] for row in rows] == ["月季花苗", "月季花肥料", "藤本月季"]
    top = rows[0]
    assert top["rec_bid"] == 1.85
    assert top["pc_pv"] == 2100
    assert top["bid_pc"] == 2.1
    assert top["reasons"] == ["搜索意图匹配"]


async def test_expand_respects_limit() -> None:
    client = transport({"KRService/getKRByQuery": fixture_text("kr_expand.json")})

    rows = await keyword.expand(client, "月季", limit=2)

    assert len(rows) == 2


async def test_expand_rejects_empty_seed() -> None:
    client = transport({})

    with pytest.raises(ValueError, match="种子词"):
        await keyword.expand(client, "  ")


async def test_plan_dedupes_across_seeds() -> None:
    client = transport({"KRService/getKRByQuery": fixture_text("kr_expand.json")})

    result = await keyword.plan(client, ["月季", "藤本"])

    assert result["total"] == 3
    assert result["seeds"] == [
        {"seed": "月季", "returned": 3, "new": 3},
        {"seed": "藤本", "returned": 3, "new": 0},
    ]
    assert result["coverage"]["cap_known"] is False
    assert "不保证全量" in result["coverage"]["note"]
    assert result["truncated"] is False


async def test_plan_marks_truncation_when_limit_hit() -> None:
    client = transport({"KRService/getKRByQuery": fixture_text("kr_expand.json")})

    result = await keyword.plan(client, ["月季"], limit_per_seed=2)

    assert result["total"] == 2
    assert result["truncated"] is True


async def test_plan_requires_at_least_one_seed() -> None:
    client = transport({})

    with pytest.raises(ValueError, match="至少需要一个种子词"):
        await keyword.plan(client, ["", "  "])
