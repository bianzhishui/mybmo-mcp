"""接口目录：Service / Method / 读写 / 产品线。

数据来自 ``catalog.json``（随包分发），由 ``scripts/build_catalog.py``
从能力索引文档生成。目录本身不含任何调用逻辑，只回答三个问题：

- 这个 service 有哪些 method？
- 这个 method 是读还是写？
- 它属于哪条产品线（sms / feed）？

**目录是提示，不是白名单。** 未收录的接口仍可通过 :func:`normalize_method`
归一化后透传；但 ``api_read`` 这类只读工具会以目录为准做校验，
因此目录的准确性直接影响可用接口范围。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from mybmo_mcp.baidu.transport import is_write_method, normalize_method, normalize_service

CATALOG_FILENAME = "catalog.json"
CATALOG_ENV = "MYBMO_CATALOG_PATH"


@dataclass(frozen=True, slots=True)
class ApiMethod:
    name: str
    read: bool
    origin: str | None = None


@dataclass(frozen=True, slots=True)
class ApiService:
    name: str
    group: str
    product_line: str | None
    methods: dict[str, ApiMethod] = field(default_factory=dict)


class Catalog:
    """接口目录。构造后不可变，可安全共享。"""

    def __init__(self, services: dict[str, ApiService], meta: dict[str, Any]) -> None:
        self._services = services
        self.meta = meta

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> Catalog:
        """加载目录。

        优先级：显式 ``path`` > 环境变量 ``MYBMO_CATALOG_PATH`` > 包内 ``catalog.json``。
        """
        raw: str
        if path is not None:
            raw = Path(path).read_text(encoding="utf-8")
        elif os.environ.get(CATALOG_ENV):
            raw = Path(os.environ[CATALOG_ENV]).read_text(encoding="utf-8")  # type: ignore[arg-type]
        else:
            raw = (
                resources.files(__package__).joinpath(CATALOG_FILENAME).read_text(encoding="utf-8")
            )
        return cls.from_dict(json.loads(raw))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Catalog:
        services: dict[str, ApiService] = {}
        for name, entry in (data.get("services") or {}).items():
            methods = {
                method: ApiMethod(
                    name=method,
                    read=bool(info.get("read", not is_write_method(method))),
                    origin=info.get("origin"),
                )
                for method, info in (entry.get("methods") or {}).items()
            }
            services[name] = ApiService(
                name=name,
                group=entry.get("group", ""),
                product_line=entry.get("product_line"),
                methods=methods,
            )
        return cls(services, data.get("meta") or {})

    @property
    def stats(self) -> dict[str, int]:
        return {
            "services": len(self._services),
            "methods": sum(len(s.methods) for s in self._services.values()),
        }

    def get(self, service: str) -> ApiService | None:
        """按服务名查（先精确匹配，再归一化后匹配）。"""
        if not service:
            return None
        if service in self._services:
            return self._services[service]
        return self._services.get(normalize_service(service))

    def describe(self, service: str, method: str) -> dict[str, Any] | None:
        """查单个接口。找不到返回 ``None``，由调用方决定是报错还是透传。"""
        svc = self.get(service)
        if svc is None:
            return None
        canonical = normalize_method(method)
        info = svc.methods.get(canonical)
        if info is None:
            return None
        return {
            "service": svc.name,
            "method": info.name,
            "product_line": svc.product_line,
            "write": not info.read,
            "group": svc.group,
            "origin": info.origin,
        }

    def search(self, keyword: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """按服务名或方法名的子串检索，用于 ``api_describe`` 的模糊查询。"""
        needle = (keyword or "").strip().lower()
        if not needle:
            return []
        hits: list[dict[str, Any]] = []
        for svc in self._services.values():
            for info in svc.methods.values():
                if needle in svc.name.lower() or needle in info.name.lower():
                    hits.append(
                        {
                            "service": svc.name,
                            "method": info.name,
                            "product_line": svc.product_line,
                            "write": not info.read,
                            "group": svc.group,
                        }
                    )
                    if len(hits) >= limit:
                        return hits
        return hits
