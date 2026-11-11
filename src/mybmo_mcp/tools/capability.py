"""能力网关三件套：检索、取 schema、执行。

这三个工具构成对外的完整能力面——389 条接口全靠它们，工具列表不随接口增长。

设计要点：

- ``search_capabilities`` 只返回**短摘要**，不返回参数。参数可能几十个字段，
  一次返回十条会把上下文撑爆。
- ``get_capability_schema`` 返回该能力**用到的**模型定义（``$ref`` 闭包），
  不是全部 585 个。
- ``execute_capability`` 负责把 agent 给的参数对齐到线上字段名：接受 snake_case，
  自动转驼峰；完全不认识的字段直接拒绝并给出候选，避免带着错字段名打到百度。
"""

from __future__ import annotations

import asyncio
import difflib
import time
from typing import Any

from mcp.server import MCPServer

from mybmo_mcp.audit.log import (
    STATE_DENIED,
    STATE_FAILED,
    STATE_SUCCEEDED,
    STATE_UNKNOWN,
    new_call_id,
)
from mybmo_mcp.auth.tenancy import AccessDenied, required_permission
from mybmo_mcp.baidu.capabilities import AsyncTask, Capability, CapabilityRegistry
from mybmo_mcp.baidu.errors import BaiduApiError, BaiduError
from mybmo_mcp.tools._result import fail, fail_from, guarded, ok
from mybmo_mcp.tools.runtime import get_runtime


async def search_capabilities(query: str, limit: int = 10) -> dict[str, Any]:
    """按中文或英文关键词检索能力，返回候选清单（只有摘要，不含参数）。

    用法：先在这里找，再用 ``get_capability_schema`` 取参数，最后 ``execute_capability`` 执行。
    中文可以直接说"拓词""加词""拉报表""改预算"；也可以给服务名或方法名。
    """

    async def _run() -> dict[str, Any]:
        registry = get_runtime().capabilities
        found = registry.search(query, limit=max(1, min(limit, 50)))
        return {
            "query": query,
            "count": len(found),
            "capabilities": [cap.to_summary() for cap in found],
            "hint": "" if found else "换个更短的关键词，或直接用 Service.method 形式检索",
        }

    return await guarded(_run)


async def get_capability_schema(capability: str) -> dict[str, Any]:
    """取某个能力的完整定义：端点、读写标记、参数 JSON Schema 与相关模型。

    ``capability`` 用 ``search_capabilities`` 返回的 key，例如 ``KRService.getKRByQuery``。
    """
    registry = get_runtime().capabilities
    cap = registry.get(capability)
    if cap is None:
        return _not_found(registry, capability)

    # 手写要点在前：单位、层级、前置条件这类信息比通用提示重要得多
    notes = [*cap.notes, "参数名用驼峰（如 campaignName）；传 snake_case 会自动转换"]
    input_schema = registry.resolved_input_schema(cap)
    if not (input_schema.get("properties") or {}):
        notes.append("该能力不需要业务参数，input 传空对象即可")

    return ok(
        {
            "capability": cap.key,
            "summary": cap.summary,
            "description": cap.description,
            "endpoint": cap.endpoint,
            "product_line": cap.product_line,
            "mutating": cap.mutating,
            "input_schema": input_schema,
            "schemas": registry.schema_closure(cap),
            "notes": notes,
        }
    )


async def execute_capability(
    capability: str,
    input: dict[str, Any] | None = None,
    account: str | None = None,
    wait: bool = False,
) -> dict[str, Any]:
    """执行一个能力。``input`` 原样作为请求体业务参数发出去。

    ``account`` 传账户别名（见 list_accounts），留空只在当前身份只有一个可用账户时生效。
    ``wait=True`` 只对声明了异步任务的能力生效，其余会忽略并在 meta 里说明。

    权限按能力的读写属性判定：只读能力要 read，写能力要 write。
    ``mutating=unknown`` 按写处理——判不准时偏向更严的一侧。

    每次调用都会写一条审计，返回的 ``meta.call_id`` 是它的编号。
    结果未知（超时、5xx）会被记成 ``unknown``，此时应先查询后台状态，不要直接重发。
    """
    runtime = get_runtime()
    registry = runtime.capabilities

    cap = registry.get(capability)
    if cap is None:
        return _not_found(registry, capability)

    notes: list[str] = []
    try:
        payload = normalize_payload(cap, input or {}, registry, notes)
    except ValueError as exc:
        return fail(str(exc), hint="先用 get_capability_schema 看参数名与层级")

    permission = required_permission(cap.mutating)
    call_id = new_call_id()

    try:
        principal = runtime.current_principal()
    except BaiduError as exc:
        # 连身份都没有，无法归属审计；这不是账户级失败，直接报给调用方。
        return fail_from(exc)

    started = time.perf_counter()

    def _audit(state: str, *, alias: str = "", error: str = "", code: int | None = None) -> None:
        runtime.audit.record(
            sub=principal.sub,
            tenant_id=principal.tenant_id,
            account=alias,
            capability=cap.key,
            permission=permission,
            mutating=cap.mutating,
            state=state,
            call_id=call_id,
            duration_ms=(time.perf_counter() - started) * 1000,
            error_code=code,
            error=error,
            payload=payload,
        )

    try:
        entry = runtime.access.resolve(principal, account, permission=permission)
    except AccessDenied as exc:
        # 优先用异常里带回的 alias：不传 account 时它已经被解析成具体账户了，
        # 记成空字符串会让「谁想动哪个账户被拒」这条线索断掉。
        _audit(STATE_DENIED, alias=exc.alias or account or "", error=str(exc))
        return fail_from(exc)
    except (BaiduError, ValueError) as exc:
        _audit(STATE_FAILED, alias=account or "", error=str(exc))
        return fail_from(exc)

    try:
        transport = runtime.transport_for(entry.alias, principal=principal, permission=permission)
        data = await transport.call(cap.service, cap.method, payload, product_line=cap.product_line)
    except BaiduApiError as exc:
        state = STATE_UNKNOWN if exc.outcome == "unknown" else STATE_FAILED
        _audit(state, alias=entry.alias, error=str(exc), code=exc.code)
        return fail_from(exc)
    except (BaiduError, ValueError) as exc:
        _audit(STATE_FAILED, alias=entry.alias, error=str(exc))
        return fail_from(exc)

    # 先等异步任务再有"成功"这回事：轮询失败时不该先记一条 succeeded 再记 failed。
    wait_meta: Any = None
    if wait:
        if cap.async_task is None:
            wait_meta = "ignored：该能力未声明 async_task 元数据"
        else:
            try:
                data, wait_meta = await _await_async_task(
                    cap.async_task,
                    data,
                    transport,
                    product_line=cap.product_line,
                )
            except (BaiduApiError, BaiduError, ValueError) as exc:
                _audit(STATE_FAILED, alias=entry.alias, error=str(exc))
                return fail_from(exc)

    _audit(STATE_SUCCEEDED, alias=entry.alias)

    meta: dict[str, Any] = {
        "capability": cap.key,
        "endpoint": cap.endpoint,
        "product_line": cap.product_line,
        "mutating": cap.mutating,
        "required_permission": permission,
        "account": entry.alias,
        "call_id": call_id,
    }
    if notes:
        meta["normalized"] = notes
    if wait_meta is not None:
        meta["wait"] = wait_meta
    return ok(data, meta)


# ---------- 异步任务轮询 ----------


def _lookup(data: Any, path: str) -> Any:
    """按 ``a.b.c`` 取嵌套字段；取不到返回 ``None``。

    中途遇到数组时取**第一个元素**：百度不少接口把单条结果放在单元素数组里
    （``createReportTask``、``getTaskStatus`` 都是），不展开的话拿不到 ``taskId``，
    ``wait=True`` 会静默退化成"无法轮询"。
    """
    current: Any = data
    for part in path.split("."):
        if isinstance(current, list):
            current = current[0] if current else None
            if current is None:
                return None
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _is_done(data: Any, rules: tuple[tuple[str, tuple[Any, ...]], ...]) -> bool:
    if not rules:
        return True
    return all(_lookup(data, field) in values for field, values in rules)


async def _await_async_task(
    task: AsyncTask,
    created: Any,
    transport: Any,
    *,
    product_line: str,
) -> tuple[Any, dict[str, Any]]:
    """按元数据轮询到终态，必要时再取结果。

    三条约束（ADR 0004 D5）：超时**不抛错**、间隔递增、总时长有上限。
    """
    task_id = _lookup(created, task.task_id_field)
    if task_id is None:
        return created, {"ignored": f"响应里没有 {task.task_id_field}，无法轮询"}

    service, _, method = task.status_capability.partition(".")
    body = {task.task_id_field: task_id}
    deadline = time.monotonic() + task.timeout
    interval = max(0.01, task.poll_interval)
    polls = 0
    last: Any = None

    while True:
        last = await transport.call(service, method, dict(body), product_line=product_line)
        polls += 1
        if _is_done(last, task.done_when):
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return (
                {"task_id": task_id, "status": last, "timed_out": True},
                {"task_id": task_id, "polls": polls, "timed_out": True},
            )
        await asyncio.sleep(min(interval, remaining))
        interval = min(interval * 2, 10.0)

    meta: dict[str, Any] = {"task_id": task_id, "polls": polls, "timed_out": False}
    if not task.result_capability:
        return last, meta

    result_service, _, result_method = task.result_capability.partition(".")
    result = await transport.call(
        result_service, result_method, dict(body), product_line=product_line
    )
    meta["result_capability"] = task.result_capability
    meta["status"] = last
    return result, meta


# ---------- 参数对齐 ----------


def normalize_payload(
    cap: Capability,
    payload: dict[str, Any],
    registry: CapabilityRegistry,
    notes: list[str],
) -> dict[str, Any]:
    """把 agent 给的参数对齐成线上字段名，未知字段直接报错。"""
    schemas = registry.schema_closure(cap)
    normalized = _normalize(payload, cap.input_schema, schemas, "", notes)
    if not isinstance(normalized, dict):
        raise ValueError("input 必须是对象")
    return normalized


def _normalize(
    value: Any, schema: dict[str, Any], schemas: dict[str, Any], path: str, notes: list[str]
) -> Any:
    schema = _resolve(schema, schemas)

    if isinstance(value, list):
        item_schema = schema.get("items") or {}
        return [_normalize(item, item_schema, schemas, f"{path}[].", notes) for item in value]

    if not isinstance(value, dict):
        return value

    properties = schema.get("properties") or {}
    if not properties:
        return value

    result: dict[str, Any] = {}
    for key, item in value.items():
        target = key if key in properties else _camel(key)
        if target not in properties:
            candidates = difflib.get_close_matches(target, list(properties), n=3)
            hint = f"，可能想要：{', '.join(candidates)}" if candidates else ""
            available = ", ".join(list(properties)[:8])
            raise ValueError(f"{path}{key} 不是该能力的参数{hint}；可用字段：{available}")
        if target != key:
            notes.append(f"{path}{key} → {target}")
        result[target] = _normalize(item, properties[target], schemas, f"{path}{target}.", notes)
    return result


def _resolve(schema: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    for _ in range(10):
        if not isinstance(schema, dict) or "$ref" not in schema:
            break
        schema = schemas.get(schema["$ref"].rsplit("/", 1)[-1], {})
    return schema if isinstance(schema, dict) else {}


def _camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def _not_found(registry: CapabilityRegistry, capability: str) -> dict[str, Any]:
    nearby = registry.search(capability, limit=5)
    return fail(
        f"没有这个能力：{capability}",
        hint=(
            f"可能是：{', '.join(cap.key for cap in nearby)}"
            if nearby
            else "先用 search_capabilities 检索，key 形如 Service.method"
        ),
    )


def register(mcp: MCPServer) -> None:
    mcp.tool()(search_capabilities)
    mcp.tool()(get_capability_schema)
    mcp.tool()(execute_capability)
