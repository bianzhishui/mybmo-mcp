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

import difflib
from typing import Any

from mcp.server import MCPServer

from mybmo_mcp.auth.tenancy import required_permission
from mybmo_mcp.baidu.capabilities import Capability, CapabilityRegistry
from mybmo_mcp.baidu.errors import BaiduError
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

    notes = ["参数名用驼峰（如 campaignName）；传 snake_case 会自动转换"]
    input_schema = registry.resolved_input_schema(cap)
    if not (input_schema.get("properties") or {}):
        notes.append("该能力不需要业务参数，input 传空对象即可")

    return ok(
        {
            "capability": cap.key,
            "summary": cap.summary,
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
    try:
        transport = runtime.transport_for(account, permission=permission)
        data = await transport.call(cap.service, cap.method, payload, product_line=cap.product_line)
    except (BaiduError, ValueError) as exc:
        return fail_from(exc)

    meta: dict[str, Any] = {
        "capability": cap.key,
        "endpoint": cap.endpoint,
        "product_line": cap.product_line,
        "mutating": cap.mutating,
        "required_permission": permission,
    }
    if notes:
        meta["normalized"] = notes
    if wait:
        meta["wait"] = "ignored：该能力未声明 async_task 元数据"
    return ok(data, meta)


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
