"""从 `baiduads-sdk` 内省生成 ``capabilities.json``。

用法：

    uv run python scripts/build_capabilities.py

产物：``src/mybmo_mcp/baidu/capabilities.json``（随包分发，运行时不依赖 SDK）。

为什么是构建期：见 `docs/decisions/0005-sdk-poc.md`。SDK 不校验未知字段、响应反序列化
严格不可关闭，因此只取它的元数据（端点、字段树），发送交给我们自己的 transport。

产出结构：

```json
{
  "meta":        { "capabilities": 389, "schemas": 812, ... },
  "schemas":     { "ApiCampaignAddRequest": { JSON Schema } },
  "capabilities": {
    "CampaignService.addCampaign": {
      "endpoint": "/json/sms/service/CampaignService/addCampaign",
      "product_line": "sms",
      "mutating": "true",
      "group": "搜索推广投放",
      "body": "ApiCampaignAddRequest",
      "input_schema": { "type": "object", "properties": { "campaignTypes": {...} } }
    }
  }
}
```

嵌套结构**保持原样**（`campaignTypes[].campaignName` 不展开成点路径），
这样 ``execute_capability`` 收到的 ``input`` 可以原样作为请求体发出。
"""

from __future__ import annotations

import importlib
import json
import pkgutil
from datetime import date, datetime
from pathlib import Path
from typing import Any

import baiduads

ROOT = Path(__file__).resolve().parents[1]
GROUPS = ROOT / "scripts" / "service_groups.json"
OUTPUT = ROOT / "src" / "mybmo_mcp" / "baidu" / "capabilities.json"

#: 明确会改变投放或账户配置的动词
MUTATING_VERBS = (
    "add",
    "update",
    "delete",
    "del",
    "mod",
    "set",
    "create",
    "save",
    "copy",
    "submit",
    "bind",
    "unbind",
    "push",
    "upload",
    "cancel",
    "accept",
    "reject",
    "confirm",
    "deliver",
    "take",
    "remove",
    "grant",
    "refresh",
    "abort",
    "init",
    "batch",
    "bat",
    "clip",
    "smart",
    "fast",
    "edit",
    "agree",
)

#: 明确只读的动词
READ_VERBS = (
    "get",
    "query",
    "list",
    "search",
    "describe",
    "download",
    "check",
    "count",
    "decode",
    "preview",
    "export",
)

#: 读方法常见的名词后缀。用于 `synthetiseStatus` 这类"动词看起来像写、实际查状态"的情况
READ_SUFFIXES = ("status", "info", "list", "detail", "result", "record")

SCALAR_SCHEMA: dict[type, dict[str, Any]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    datetime: {"type": "string", "format": "date-time"},
    date: {"type": "string", "format": "date"},
}


def is_model(cls: Any) -> bool:
    return isinstance(cls, type) and hasattr(cls, "openapi_types") and hasattr(cls, "attribute_map")


def judge_mutating(method: str) -> str:
    """三态判定。判不准就返回 ``unknown``，不要猜成任何一种。"""
    name = method.lower()
    if name.startswith(MUTATING_VERBS):
        return "true"
    if name.startswith(READ_VERBS):
        return "false"
    if name.endswith(READ_SUFFIXES):
        return "false"
    return "unknown"


class SchemaBuilder:
    """把 OpenAPI 生成的模型类转成 JSON Schema，递归下钻并去重。"""

    def __init__(self) -> None:
        self.schemas: dict[str, dict[str, Any]] = {}
        self._visiting: set[str] = set()

    def build(self, cls: Any) -> dict[str, Any]:
        if not is_model(cls):
            return self._scalar(cls) or {}

        name = cls.__name__
        if name in self.schemas and name not in self._visiting:
            return {"$ref": f"#/schemas/{name}"}
        if name in self._visiting:  # 自引用，避免无限递归
            return {"type": "object", "description": "（递归引用，展开见 schemas）"}

        self._visiting.add(name)
        properties: dict[str, Any] = {}
        required: list[str] = []

        for attr, allowed in cls.openapi_types.items():
            wire = cls.attribute_map.get(attr, attr)
            properties[wire] = self._type_schema(allowed)
            # 生成器没有必填信息，保守起见全部标为可选

        self._visiting.discard(name)
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        self.schemas[name] = schema
        return {"$ref": f"#/schemas/{name}"}

    def _type_schema(self, allowed: Any) -> dict[str, Any]:
        types = allowed if isinstance(allowed, tuple) else (allowed,)
        nullable = False
        candidates: list[dict[str, Any]] = []

        for item in types:
            if item is type(None):
                nullable = True
                continue
            if isinstance(item, list):  # [X] 表示数组
                element = self._type_schema(item[0]) if item else {}
                candidates.append({"type": "array", "items": element})
                continue
            if is_model(item):
                candidates.append(self.build(item))
                continue
            scalar = self._scalar(item)
            if scalar:
                candidates.append(scalar)

        if not candidates:
            return {"description": "（未知类型）"}
        result = candidates[0] if len(candidates) == 1 else {"anyOf": candidates}
        if nullable:
            result = {**result, "nullable": True}
        return result

    def _scalar(self, cls: Any) -> dict[str, Any] | None:
        return SCALAR_SCHEMA.get(cls)


def discover_service_packages() -> list[str]:
    return sorted(
        m.name
        for m in pkgutil.iter_modules(baiduads.__path__)
        if m.ispkg and m.name not in {"common", "api"}
    )


def collect(package: str, groups: dict[str, str], builder: SchemaBuilder) -> tuple[dict, list[str]]:
    """处理一个服务包，返回 {key: capability} 与告警列表。"""
    warnings: list[str] = []
    # 模块名不等于包名：campaignfeed 包下是 campaign_feed_service
    try:
        api_pkg = importlib.import_module(f"baiduads.{package}.api")
    except ModuleNotFoundError:
        return {}, warnings
    service_modules = [
        m.name for m in pkgutil.iter_modules(api_pkg.__path__) if m.name.endswith("_service")
    ]
    if not service_modules:
        return {}, warnings

    try:
        service_module = importlib.import_module(f"baiduads.{package}.api.{service_modules[0]}")
    except ModuleNotFoundError:
        return {}, warnings

    service_cls = next(
        (
            obj
            for name, obj in vars(service_module).items()
            if name.endswith("Service") and isinstance(obj, type)
        ),
        None,
    )
    if service_cls is None:
        return {}, warnings

    try:
        service = service_cls()  # 只构造，不发请求
    except Exception as exc:  # pragma: no cover - 生成期防护
        return {}, [f"{package}: 实例化失败 {type(exc).__name__}"]

    capabilities: dict[str, dict[str, Any]] = {}
    for attr in dir(service):
        if not attr.endswith("_endpoint"):
            continue
        endpoint = getattr(service, attr)
        settings = getattr(endpoint, "settings", None) or {}
        path = settings.get("endpoint_path")
        if not path:
            continue

        snake = attr[: -len("_endpoint")]
        # 方法名以端点路径最后一段为准：SDK 的 operation_id 会把缩写弄坏
        # （getKRByQuery 被生成为 get_krby_query），路径才是真实方法名
        method = path.rsplit("/", 1)[-1] or settings.get("operation_id") or _camel(snake)
        key = f"{service_cls.__name__}.{method}"

        body_cls = _body_class(package, snake, warnings, key)
        capabilities[key] = {
            "service": service_cls.__name__,
            "method": method,
            "endpoint": path,
            "product_line": "feed" if "/json/feed" in path else "sms",
            "mutating": judge_mutating(method),
            "group": groups.get(service_cls.__name__, ""),
            "body": body_cls.__name__ if body_cls else None,
            "input_schema": builder.build(body_cls)
            if body_cls
            else {"type": "object", "properties": {}},
        }
    return capabilities, warnings


def _body_class(package: str, snake: str, warnings: list[str], key: str) -> Any | None:
    try:
        wrapper_module = importlib.import_module(
            f"baiduads.{package}.model.{snake}_request_wrapper"
        )
    except ModuleNotFoundError:
        warnings.append(f"{key}: 找不到 request_wrapper（{snake}）")
        return None

    wrapper_cls = next(
        (
            obj
            for name, obj in vars(wrapper_module).items()
            if name.endswith("RequestWrapper") and isinstance(obj, type)
        ),
        None,
    )
    if wrapper_cls is None:
        warnings.append(f"{key}: wrapper 模块里没有 *RequestWrapper 类")
        return None

    allowed = wrapper_cls.openapi_types.get("body")
    if not allowed:
        return None
    body = allowed[0] if isinstance(allowed, tuple) else allowed
    return body if is_model(body) else None


def _camel(snake: str) -> str:
    parts = snake.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def main() -> int:
    # 能力域标签是人定的归类，SDK 里没有这个信息，只能单独维护。
    # 缺一个服务就是空分组，不影响生成，检索里只是少一层权重。
    groups = json.loads(GROUPS.read_text(encoding="utf-8"))

    builder = SchemaBuilder()
    capabilities: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    for package in discover_service_packages():
        found, warns = collect(package, groups, builder)
        capabilities.update(found)
        warnings.extend(warns)

    mutating = {
        v: sum(1 for c in capabilities.values() if c["mutating"] == v)
        for v in ("true", "false", "unknown")
    }

    payload = {
        "meta": {
            "version": 1,
            "source": f"baiduads-sdk {getattr(baiduads, '__version__', '2023.1.0')}",
            "note": "构建期从 SDK 内省生成；嵌套结构保持原样，input 可直接作为请求体",
            "capabilities": len(capabilities),
            "schemas": len(builder.schemas),
            "mutating": mutating,
        },
        "schemas": dict(sorted(builder.schemas.items())),
        "capabilities": dict(sorted(capabilities.items())),
    }

    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    size_kb = OUTPUT.stat().st_size / 1024

    print(f"写入 {OUTPUT}（{size_kb:.0f} KB）")
    print(
        f"能力 {len(capabilities)} / 模型 {len(builder.schemas)} / "
        f"mutating true={mutating['true']} false={mutating['false']} unknown={mutating['unknown']}"
    )
    for warning in warnings[:10]:
        print(f"  ! {warning}")
    if len(warnings) > 10:
        print(f"  … 另有 {len(warnings) - 10} 条告警")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
