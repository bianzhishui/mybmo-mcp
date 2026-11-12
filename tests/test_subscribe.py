"""订阅接收端的三条铁律：不配密钥拒绝、验不过不落盘、落盘前脱敏。"""

from __future__ import annotations

import json

import pytest

from mybmo_mcp.subscribe import (
    MAX_BODY_BYTES,
    build_record,
    check_key,
    extract_key,
    looks_like_baidu_key,
    redact,
)

SECRET = "s3cr3t-subscribe-key"


def test_extract_key_prefers_bearer() -> None:
    value, source = extract_key(
        {"authorization": "Bearer abc", "x-mybmo-token": "def"}, {"token": "ghi"}
    )

    assert value == "abc"
    assert source == "authorization:bearer"


def test_extract_key_falls_back_to_header_then_query() -> None:
    assert extract_key({"x-mybmo-token": "h"}, {}) == ("h", "header:x-mybmo-token")
    assert extract_key({}, {"token": "q"}) == ("q", "query:token")
    assert extract_key({}, {}) == ("", "")


def test_check_key_accepts_match_and_reports_source() -> None:
    ok, source = check_key(SECRET, {"authorization": f"Bearer {SECRET}"}, {})

    assert ok is True
    assert source == "authorization:bearer"


@pytest.mark.parametrize(
    "headers,params",
    [
        ({}, {}),
        ({"authorization": "Bearer wrong"}, {}),
        ({"x-mybmo-token": "wrong"}, {}),
        ({}, {"token": "wrong"}),
    ],
)
def test_check_key_rejects_everything_else(
    headers: dict[str, str], params: dict[str, str]
) -> None:
    ok, _ = check_key(SECRET, headers, params)

    assert ok is False


def test_empty_configured_secret_never_matches() -> None:
    """没配密钥时不能"谁都放行"——这条写反了就是公网匿名写入口。"""
    ok, _ = check_key("", {"authorization": "Bearer "}, {})

    assert ok is False


def test_redact_removes_secret_only() -> None:
    assert redact(f"a {SECRET} b", SECRET) == "a *** b"
    assert redact("nothing here", SECRET) == "nothing here"


def test_build_record_keeps_body_and_redacts_secret() -> None:
    body = json.dumps({"event": "audit_result", "sig": SECRET, "id": 1}).encode()

    record = build_record(
        method="POST",
        path="/oauth/baidu/subscribe",
        headers={"authorization": f"Bearer {SECRET}", "content-type": "application/json"},
        params={"token": SECRET},
        body=body,
        secret=SECRET,
        key_source="authorization:bearer",
        client="1.2.3.4",
    )

    assert SECRET not in json.dumps(record, ensure_ascii=False)
    assert record["headers"]["content-type"] == "application/json"
    assert record["body_json"]["event"] == "audit_result"
    assert record["body_truncated"] is False
    assert record["key_source"] == "authorization:bearer"
    assert record["client"] == "1.2.3.4"


def test_build_record_truncates_oversized_body() -> None:
    record = build_record(
        method="POST",
        path="/p",
        headers={},
        params={},
        body=b"x" * (MAX_BODY_BYTES + 10),
        secret=SECRET,
        key_source="header:x-mybmo-token",
    )

    assert record["body_truncated"] is True
    assert len(record["body"]) == MAX_BODY_BYTES


def test_build_record_tolerates_non_json_body() -> None:
    record = build_record(
        method="GET",
        path="/p",
        headers={},
        params={},
        body=b"event=paused&id=42",
        secret=SECRET,
        key_source="query:token",
    )

    assert record["body_json"] is None
    assert record["body"] == "event=paused&id=42"



def test_looks_like_baidu_key_accepts_32_lowercase_alnum() -> None:
    assert looks_like_baidu_key("0123456789abcdefghijklmnopqrstuv") is True


@pytest.mark.parametrize(
    "value",
    [
        "0123456789ABCDEFGHIJKLMNOPQRSTUV",  # 含大写
        "0123456789abcdefghijklmnopqrstu",  # 31 位
        "0123456789abcdefghijklmnopqrstuvw",  # 33 位
        "0123456789abcdefghijklmnopqrstu-",  # 含符号
        "",
    ],
)
def test_looks_like_baidu_key_rejects_other_shapes(value: str) -> None:
    assert looks_like_baidu_key(value) is False
