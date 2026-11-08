from __future__ import annotations

from typing import Any

import pytest

from mybmo_mcp.baidu.capabilities import AsyncTask, CapabilityRegistry
from mybmo_mcp.tools.capability import _await_async_task, _is_done, _lookup


def task(**overrides: Any) -> AsyncTask:
    base = dict(
        status_capability="ReportService.getTaskStatus",
        result_capability="",
        task_id_field="taskId",
        done_when=(("status", (3,)),),
        poll_interval=0.01,
        timeout=1.0,
    )
    base.update(overrides)
    return AsyncTask(**base)  # type: ignore[arg-type]


class ScriptedTransport:
    """按顺序返回预设响应，并记录每次调用。"""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
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
        return self._responses.pop(0) if self._responses else {"status": 9}


# ---------- 元数据解析 ----------


def test_async_task_parsed_from_metadata() -> None:
    parsed = AsyncTask.from_dict(
        {
            "status_capability": "OpenapiReportService.getTaskStatus",
            "result_capability": "OpenapiReportService.getReportData",
            "task_id_field": "taskId",
            "done_when": {"status": [3]},
            "poll_interval": 2,
            "timeout": 120,
        }
    )
    assert parsed is not None
    assert parsed.status_capability == "OpenapiReportService.getTaskStatus"
    assert parsed.done_when == (("status", (3,)),)
    assert parsed.timeout == 120


def test_async_task_rejects_malformed_metadata() -> None:
    assert AsyncTask.from_dict({"status_capability": "getTaskStatus"}) is None
    assert AsyncTask.from_dict({}) is None


def test_capability_registry_keeps_async_task() -> None:
    registry = CapabilityRegistry.from_dict(
        {
            "capabilities": {
                "Report.create": {
                    "service": "Report",
                    "method": "create",
                    "endpoint": "https://api.baidu.com/x",
                    "product_line": "sms",
                    "mutating": "true",
                    "async_task": {
                        "status_capability": "Report.getTaskStatus",
                        "done_when": {"status": [3]},
                    },
                }
            },
            "schemas": {},
        }
    )
    cap = registry.get("Report.create")
    assert cap is not None
    assert cap.async_task is not None
    assert cap.async_task.status_capability == "Report.getTaskStatus"


# ---------- 字段判定 ----------


def test_lookup_supports_nested_path() -> None:
    assert _lookup({"data": {"task": {"id": 7}}}, "data.task.id") == 7
    assert _lookup({"data": None}, "data.task") is None
    assert _lookup([], "a") is None


def test_done_when_requires_all_rules() -> None:
    rules = (("status", (3, 4)), ("progress", (100,)))
    assert _is_done({"status": 3, "progress": 100}, rules)
    assert not _is_done({"status": 3, "progress": 50}, rules)
    assert _is_done({"anything": 1}, ())  # 没有规则视为立即完成


# ---------- 轮询 ----------


async def test_await_returns_immediately_when_done() -> None:
    transport = ScriptedTransport([{"status": 3, "rows": 10}])
    data, meta = await _await_async_task(
        task(), {"taskId": "t-1"}, transport, product_line="sms"
    )
    assert data == {"status": 3, "rows": 10}
    assert meta == {"task_id": "t-1", "polls": 1, "timed_out": False}
    assert transport.calls[0]["body"] == {"taskId": "t-1"}


async def test_await_polls_until_done_then_fetches_result() -> None:
    transport = ScriptedTransport(
        [{"status": 1}, {"status": 2}, {"status": 3}, {"rows": [1, 2, 3]}]
    )
    data, meta = await _await_async_task(
        task(result_capability="Report.getData"),
        {"taskId": "t-2"},
        transport,
        product_line="sms",
    )
    assert data == {"rows": [1, 2, 3]}
    assert meta["polls"] == 3
    assert meta["timed_out"] is False
    assert [c["method"] for c in transport.calls] == [
        "getTaskStatus",
        "getTaskStatus",
        "getTaskStatus",
        "getData",
    ]


async def test_await_times_out_without_raising() -> None:
    transport = ScriptedTransport([{"status": 1}] * 1000)
    data, meta = await _await_async_task(
        task(timeout=0.05), {"taskId": "t-3"}, transport, product_line="sms"
    )
    assert meta["timed_out"] is True
    assert data["timed_out"] is True
    assert data["task_id"] == "t-3"
    assert meta["polls"] >= 1


async def test_await_ignored_when_task_id_missing() -> None:
    transport = ScriptedTransport([])
    created = {"status": 0}
    data, meta = await _await_async_task(task(), created, transport, product_line="sms")
    assert data is created
    assert "ignored" in meta
    assert transport.calls == []


async def test_await_propagates_api_errors() -> None:
    class Boom:
        async def call(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await _await_async_task(task(), {"taskId": "t-4"}, Boom(), product_line="sms")
