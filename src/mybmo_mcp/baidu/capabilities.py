"""能力注册表：加载 ``capabilities.json``，提供检索与 schema 查询。

文件由 ``scripts/build_capabilities.py`` 在构建期生成，运行时不依赖 SDK。

检索是这套网关的关键：389 条能力里，模型只会用自然语言说"帮我拓词"，
不会说 ``KRService.getKRByQuery``。所以索引里除了 key / 服务名 / 方法名，
还要有**中文别名**——否则子串匹配在中文上几乎无效。

别名分两层：服务级（该服务下所有方法都命中）与方法级（更精确）。
先手工维护高频的几十条，其余边用边补。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any

CAPABILITIES_FILENAME = "capabilities.json"
DESCRIPTIONS_FILENAME = "descriptions.json"
CAPABILITIES_ENV = "MYBMO_CAPABILITIES_PATH"
DESCRIPTIONS_ENV = "MYBMO_DESCRIPTIONS_PATH"

#: descriptions.json 的格式版本
DESCRIPTIONS_VERSION = 1

#: 服务级别名：命中后该服务下所有方法都加分
SERVICE_ALIASES: dict[str, tuple[str, ...]] = {
    "KRService": ("拓词", "关键词规划", "关键词拓展", "种子词", "关键词推荐", "搜索量"),
    "KeywordService": ("关键词", "加词", "改词", "删词", "出价"),
    "CampaignService": ("计划", "推广计划", "预算"),
    "AdgroupService": ("单元", "推广单元"),
    "CreativeService": ("创意", "标题", "描述"),
    "AccountService": ("账户", "账户信息", "账户资料"),
    "BalanceService": ("余额", "账户余额", "充值"),
    "PaymentService": ("流水", "消费记录", "付款"),
    # 注意大小写：SDK 里是 OpenApi，写成 Openapi 会让「报表」整组检索失效
    "OpenApiReportService": ("报表", "报告", "数据报表", "拉数据"),
    "SearchService": ("检索", "查询 id", "按条件找"),
    "BulkJobService": ("批量", "导出", "全量"),
    "NegativeWordService": ("否词", "否定关键词"),
    "CrowdService": ("人群", "定向人群"),
    "MaterialArticleService": ("文章", "素材"),
    "DpaApiProductSetService": ("商品集", "DPA"),
    "CampaignFeedService": ("信息流计划",),
    "AdgroupFeedService": ("信息流单元",),
    "CreativeFeedService": ("信息流创意",),
}

#: 方法级别名：更精确的高频动作
CAPABILITY_ALIASES: dict[str, tuple[str, ...]] = {
    "KRService.getKRByQuery": ("拓词", "找词", "关键词挖掘", "关键词推荐"),
    "KRService.getKRCustom": ("自定义拓词",),
    "KeywordService.addWord": ("加词", "新增关键词", "投放关键词"),
    "KeywordService.updateWord": ("改出价", "调整出价", "改关键词"),
    "KeywordService.deleteWord": ("删词", "删除关键词"),
    "CampaignService.addCampaign": ("新建计划", "建计划"),
    "CampaignService.updateCampaign": ("改预算", "调预算", "暂停计划"),
    "CampaignService.getCampaign": ("查计划", "计划列表"),
    "AdgroupService.updateAdgroup": ("调单元出价", "改单元"),
    "OpenApiReportService.getReportData": ("拉报表", "取报表数据"),
    "OpenApiReportService.createReportTask": ("建报表任务", "生成报表"),
    "AccountService.getAccountInfo": ("账户信息", "账户概览"),
    "BalanceService.getBalanceInfo": ("查余额", "余额查询"),
}


@dataclass(frozen=True, slots=True)
class Capability:
    key: str
    service: str
    method: str
    endpoint: str
    product_line: str
    mutating: str
    group: str
    body: str | None
    input_schema: dict[str, Any]

    #: 手写的一句话说明（来自 ``descriptions.json``）。为空时退回"分组｜key"。
    description: str = ""
    #: 手写要点：单位、层级、前置条件、容易踩的坑
    notes: tuple[str, ...] = ()

    @property
    def is_mutating(self) -> bool | None:
        return {"true": True, "false": False}.get(self.mutating)

    @property
    def summary(self) -> str:
        """一句话摘要，给 ``search_capabilities`` 用。不展开参数。

        有手写说明时优先用它——模型在检索结果里看到"新建推广计划"
        比看到"投放管理｜CampaignService.addCampaign"有用得多。
        """
        flag = {"true": "写", "false": "读"}.get(self.mutating, "读写未知")
        if self.description:
            return f"{self.description}｜{flag}"
        return f"{self.group or '未分组'}｜{self.key}｜{flag}"

    def to_summary(self) -> dict[str, Any]:
        return {
            "capability": self.key,
            "summary": self.summary,
            "mutating": self.mutating,
            "product_line": self.product_line,
        }


class CapabilityRegistry:
    def __init__(
        self,
        capabilities: dict[str, Capability],
        schemas: dict[str, Any],
        *,
        extra_aliases: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._schemas = schemas
        self._extra_aliases = dict(extra_aliases or {})
        self._aliases = {key: self._alias_bundle(cap) for key, cap in capabilities.items()}
        self._search_text = {key: self._build_search_text(cap) for key, cap in capabilities.items()}

    # ---------- 加载 ----------

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str] | None = None,
        descriptions_path: str | os.PathLike[str] | None = None,
    ) -> CapabilityRegistry:
        if path is not None:
            raw = Path(path).read_text(encoding="utf-8")
        elif os.environ.get(CAPABILITIES_ENV):
            raw = Path(os.environ[CAPABILITIES_ENV]).read_text(encoding="utf-8")  # type: ignore[arg-type]
        else:
            raw = (
                resources.files(__package__)
                .joinpath(CAPABILITIES_FILENAME)
                .read_text(encoding="utf-8")
            )
        return cls.from_dict(json.loads(raw), descriptions=_read_descriptions(descriptions_path))

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], descriptions: dict[str, Any] | None = None
    ) -> CapabilityRegistry:
        schemas = data.get("schemas") or {}
        capabilities = {
            key: Capability(
                key=key,
                service=entry["service"],
                method=entry["method"],
                endpoint=entry["endpoint"],
                product_line=entry["product_line"],
                mutating=entry["mutating"],
                group=entry.get("group", ""),
                body=entry.get("body"),
                input_schema=entry.get("input_schema") or {"type": "object", "properties": {}},
            )
            for key, entry in (data.get("capabilities") or {}).items()
        }
        extra_aliases: dict[str, tuple[str, ...]] = {}
        if descriptions:
            capabilities, extra_aliases = _apply_descriptions(capabilities, schemas, descriptions)
        return cls(capabilities, schemas, extra_aliases=extra_aliases)

    # ---------- 查询 ----------

    @property
    def stats(self) -> dict[str, int]:
        return {
            "capabilities": len(self._capabilities),
            "schemas": len(self._schemas),
            "services": len({cap.service for cap in self._capabilities.values()}),
            "described": sum(1 for cap in self._capabilities.values() if cap.description),
        }

    def validate_aliases(self) -> list[str]:
        """返回对不上的别名 key。

        别名表写错不会报错、只会让检索悄悄失效（``Openapi`` vs ``OpenApi`` 就
        让「报表」整组搜不到）。运行时不能因为一个别名拼错就拒绝启动，
        但测试必须能发现它。
        """
        services = {cap.service for cap in self._capabilities.values()}
        dangling = [f"服务级别名：{name}" for name in SERVICE_ALIASES if name not in services]
        dangling += [
            f"方法级别名：{key}"
            for key in (*CAPABILITY_ALIASES, *self._extra_aliases)
            if key not in self._capabilities
        ]
        return dangling

    def get(self, key: str) -> Capability | None:
        if key in self._capabilities:
            return self._capabilities[key]
        lowered = key.lower()
        for candidate, cap in self._capabilities.items():
            if candidate.lower() == lowered:
                return cap
        return None

    def resolved_input_schema(self, capability: Capability) -> dict[str, Any]:
        """把顶层 ``$ref`` 解开，返回可直接阅读的对象 schema。

        产物里 ``input_schema`` 是 ``{"$ref": ...}``（为了去重压体积），
        但工具输出给 agent 时必须是能直接看到字段名的结构。
        """
        return _resolve_node(capability.input_schema, self._schemas)

    def schema_closure(self, capability: Capability) -> dict[str, Any]:
        """返回该能力引用的全部模型定义，逐一解析 ``$ref``。

        只带用得上的那一部分，不把 585 个模型整体塞给模型。
        """
        collected: dict[str, Any] = {}
        pending = list(_refs_in(capability.input_schema))
        while pending:
            name = pending.pop()
            if name in collected or name not in self._schemas:
                continue
            collected[name] = self._schemas[name]
            pending.extend(_refs_in(self._schemas[name]))
        return collected

    def search(self, query: str, *, limit: int = 10) -> list[Capability]:
        tokens = [t for t in (query or "").lower().split() if t]
        if not tokens:
            return []

        scored: list[tuple[float, Capability]] = []
        for key, cap in self._capabilities.items():
            score = sum(
                _score(token, key, cap, self._search_text[key], self._aliases[key])
                for token in tokens
            )
            if score > 0:
                scored.append((score, cap))

        scored.sort(key=lambda item: (-item[0], item[1].key))
        return [cap for _, cap in scored[:limit]]

    # ---------- 内部 ----------

    def _alias_bundle(self, cap: Capability) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """返回 (服务级别名, 方法级别名)。描述文件可以为单个能力追加别名。"""
        service = SERVICE_ALIASES.get(cap.service, ())
        method = CAPABILITY_ALIASES.get(cap.key, ()) + self._extra_aliases.get(cap.key, ())
        return tuple(service), tuple(method)

    def _build_search_text(self, cap: Capability) -> str:
        service_aliases, method_aliases = self._aliases[cap.key]
        parts = [
            cap.key.lower(),
            cap.service.lower(),
            cap.method.lower(),
            cap.group,
            cap.description,
            *service_aliases,
            *method_aliases,
        ]
        return " ".join(part for part in parts if part)


def _score(
    token: str,
    key: str,
    cap: Capability,
    haystack: str,
    aliases: tuple[tuple[str, ...], tuple[str, ...]],
) -> float:
    service_aliases, method_aliases = aliases
    lowered_key = key.lower()
    if token == lowered_key:
        return 1000.0
    if token in {cap.method.lower(), cap.service.lower()}:
        return 900.0
    if token == key.rsplit(".", 1)[-1].lower():
        return 900.0

    score = 0.0
    if token in cap.method.lower():
        score = max(score, 700.0)
    if token in cap.service.lower():
        score = max(score, 600.0)
    if token in " ".join(method_aliases):
        score = max(score, 500.0)
    if token in " ".join(service_aliases):
        score = max(score, 400.0)
    if token in cap.group:
        score = max(score, 200.0)
    if score == 0.0 and token in haystack:
        score = max(score, 50.0)
    return score


def _refs_in(node: Any) -> set[str]:
    found: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            ref = current.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/schemas/"):
                found.add(ref.rsplit("/", 1)[-1])
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


# ---------- 手写描述 ----------


def _read_descriptions(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """读手写描述。文件不存在就返回空——描述是增强，不是启动前提。"""
    if path is not None:
        raw = Path(path).read_text(encoding="utf-8")
    elif os.environ.get(DESCRIPTIONS_ENV):
        raw = Path(os.environ[DESCRIPTIONS_ENV]).read_text(encoding="utf-8")  # type: ignore[arg-type]
    else:
        try:
            raw = (
                resources.files(__package__)
                .joinpath(DESCRIPTIONS_FILENAME)
                .read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{DESCRIPTIONS_FILENAME} 顶层必须是对象")
    return data


def _apply_descriptions(
    capabilities: dict[str, Capability],
    schemas: dict[str, Any],
    descriptions: dict[str, Any],
) -> tuple[dict[str, Capability], dict[str, tuple[str, ...]]]:
    """把手写描述合并进能力表，并把参数说明写进 schema 的 ``description``。

    引用不存在的能力直接报错——写错一个 key 就静默失效，比不写更糟：
    维护的人会以为已经写过了。
    """
    version = descriptions.get("version", DESCRIPTIONS_VERSION)
    if version != DESCRIPTIONS_VERSION:
        raise ValueError(
            f"{DESCRIPTIONS_FILENAME} 版本 {version} 不受支持（期望 {DESCRIPTIONS_VERSION}）"
        )

    entries = descriptions.get("capabilities") or {}
    if not isinstance(entries, dict):
        raise ValueError(f"{DESCRIPTIONS_FILENAME} 的 capabilities 必须是对象")

    unknown = sorted(set(entries) - set(capabilities))
    if unknown:
        shown = "、".join(unknown[:5]) + ("…" if len(unknown) > 5 else "")
        raise ValueError(
            f"{DESCRIPTIONS_FILENAME} 引用了不存在的能力：{shown}。"
            "多半是拼写错误或接口改名，改描述文件，不要改能力表"
        )

    extra_aliases: dict[str, tuple[str, ...]] = {}
    updated = dict(capabilities)
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{DESCRIPTIONS_FILENAME} 的 {key} 必须是对象")
        capability = capabilities[key]

        for param_path, text in (entry.get("params") or {}).items():
            _set_param_description(schemas, capability.input_schema, param_path, str(text), key)

        aliases = tuple(str(alias) for alias in (entry.get("aliases") or ()))
        if aliases:
            extra_aliases[key] = aliases

        updated[key] = replace(
            capability,
            description=str(entry.get("description") or ""),
            notes=tuple(str(note) for note in (entry.get("notes") or ())),
            mutating=str(entry.get("mutating") or capability.mutating),
        )
    return updated, extra_aliases


def _set_param_description(
    schemas: dict[str, Any],
    schema: dict[str, Any],
    path: str,
    text: str,
    capability_key: str,
) -> None:
    """按 ``campaignTypes[].campaignName`` 这样的路径，把说明写到字段上。

    写在解析后的模型定义上（而不是 ``$ref`` 旁边），这样 ``get_capability_schema``
    返回的 ``input_schema`` 和 ``schemas`` 两处都能看到。
    """
    node = _resolve_node(schema, schemas)
    segments = [segment for segment in path.split(".") if segment]
    if not segments:
        raise ValueError(f"{DESCRIPTIONS_FILENAME} 的 {capability_key} 有空参数路径")

    for segment in segments:
        is_array = segment.endswith("[]")
        name = segment[:-2] if is_array else segment
        properties = node.get("properties") or {}
        if name not in properties:
            raise ValueError(
                f"{DESCRIPTIONS_FILENAME} 的 {capability_key} 引用了不存在的参数：{path}"
                f"（卡在 {name}，可用字段：{', '.join(list(properties)[:6]) or '无'}）"
            )
        node = _resolve_node(properties[name], schemas)
        if is_array:
            node = _resolve_node(node.get("items") or {}, schemas)

    if not node:
        raise ValueError(f"{DESCRIPTIONS_FILENAME} 的 {capability_key} 参数 {path} 解析为空")
    node["description"] = text


def _resolve_node(node: Any, schemas: dict[str, Any]) -> dict[str, Any]:
    """沿着 ``$ref`` 一路解析到实体定义。解析不出来返回 ``{}``。"""
    for _ in range(10):
        if isinstance(node, dict) and "$ref" in node:
            node = schemas.get(str(node["$ref"]).rsplit("/", 1)[-1], {})
        else:
            break
    return node if isinstance(node, dict) else {}
