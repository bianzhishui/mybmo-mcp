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
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

CAPABILITIES_FILENAME = "capabilities.json"
CAPABILITIES_ENV = "MYBMO_CAPABILITIES_PATH"

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
    "OpenapiReportService": ("报表", "报告", "数据报表", "拉数据"),
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
    "OpenapiReportService.getReportData": ("拉报表", "取报表数据"),
    "OpenapiReportService.createReportTask": ("建报表任务", "生成报表"),
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

    @property
    def is_mutating(self) -> bool | None:
        return {"true": True, "false": False}.get(self.mutating)

    @property
    def summary(self) -> str:
        """一句话摘要，给 ``search_capabilities`` 用。不展开参数。"""
        flag = {"true": "写", "false": "读"}.get(self.mutating, "读写未知")
        group = self.group or "未分组"
        return f"{group}｜{self.key}（{flag}）"

    def to_summary(self) -> dict[str, Any]:
        return {
            "capability": self.key,
            "summary": self.summary,
            "mutating": self.mutating,
            "product_line": self.product_line,
        }


class CapabilityRegistry:
    def __init__(self, capabilities: dict[str, Capability], schemas: dict[str, Any]) -> None:
        self._capabilities = capabilities
        self._schemas = schemas
        self._search_text = {key: self._build_search_text(cap) for key, cap in capabilities.items()}

    # ---------- 加载 ----------

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> CapabilityRegistry:
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
        return cls.from_dict(json.loads(raw))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapabilityRegistry:
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
        return cls(capabilities, data.get("schemas") or {})

    # ---------- 查询 ----------

    @property
    def stats(self) -> dict[str, int]:
        return {
            "capabilities": len(self._capabilities),
            "schemas": len(self._schemas),
            "services": len({cap.service for cap in self._capabilities.values()}),
        }

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
        schema: Any = capability.input_schema
        for _ in range(10):
            if not isinstance(schema, dict) or "$ref" not in schema:
                break
            schema = self._schemas.get(str(schema["$ref"]).rsplit("/", 1)[-1], {})
        return schema if isinstance(schema, dict) else {}

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
            score = sum(_score(token, key, cap, self._search_text[key]) for token in tokens)
            if score > 0:
                scored.append((score, cap))

        scored.sort(key=lambda item: (-item[0], item[1].key))
        return [cap for _, cap in scored[:limit]]

    # ---------- 内部 ----------

    @staticmethod
    def _build_search_text(cap: Capability) -> str:
        parts = [
            cap.key.lower(),
            cap.service.lower(),
            cap.method.lower(),
            cap.group,
            *SERVICE_ALIASES.get(cap.service, ()),
            *CAPABILITY_ALIASES.get(cap.key, ()),
        ]
        return " ".join(parts)


def _score(token: str, key: str, cap: Capability, haystack: str) -> float:
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
    if token in " ".join(CAPABILITY_ALIASES.get(cap.key, ())):
        score = max(score, 500.0)
    if token in " ".join(SERVICE_ALIASES.get(cap.service, ())):
        score = max(score, 400.0)
    if token in cap.group:
        score = max(score, 200.0)
    if score == 0.0 and token in haystack:
        score = 50.0
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
