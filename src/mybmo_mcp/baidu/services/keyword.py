"""关键词规划师（KRService）。

对应接口：``KRService/getKRByQuery``。

三个易错点，都来自真实调用：

1. ``body.query`` 是**字符串**不是数组，传数组报 ``99912``；空 body 报 ``901701``。
2. 返回字段名不统一（``recBid`` / ``recommendPricePc`` / ``pcPV`` 混着来），
   这里统一归一化成扁平结构，上层不关心原始键名。
3. 单次响应**不等于全量**。接口单次上限未实测（既有资料称 300），
   因此结果里必须带 coverage 说明，不能默认模型会自己意识到。
"""

from __future__ import annotations

from typing import Any

from mybmo_mcp.baidu.errors import BaiduApiError
from mybmo_mcp.baidu.transport import BaiduTransport

SERVICE = "KRService"
METHOD = "getKRByQuery"

#: 原始字段 -> 归一化字段。改这里等于改对外契约，谨慎。
FIELD_MAP = {
    "word": "word",
    "pv": "pv",
    "show": "show",
    "click": "click",
    "competition": "competition",
    "recBid": "rec_bid",
    "pcPV": "pc_pv",
    "mobilePV": "mobile_pv",
    "groupName": "group",
    "wordPackage": "package",
    "recommendPricePc": "bid_pc",
    "recommendPriceMobile": "bid_mobile",
}

COVERAGE = {
    "single_response": True,
    "cap_known": False,
    "note": "仅为单次接口响应；接口单次上限未实测（既有资料称 300），不保证全量",
}


def normalize_word(item: dict[str, Any]) -> dict[str, Any]:
    """把一个词对象压平成对外结构。缺失字段保持 ``None``，不猜值。"""
    row: dict[str, Any] = {dst: item.get(src) for src, dst in FIELD_MAP.items()}
    row["reasons"] = item.get("showReasons") or []
    row["similar"] = item.get("similar") or []
    return row


async def expand(transport: BaiduTransport, seed: str, *, limit: int = 0) -> list[dict[str, Any]]:
    """单种子词拓词，按搜索量降序。``limit > 0`` 时截断。"""
    keyword = (seed or "").strip()
    if not keyword:
        raise ValueError("种子词不能为空")

    data = await transport.call(SERVICE, METHOD, {"query": keyword})
    if data is None:
        data = []
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise BaiduApiError(
            f"{SERVICE}/{METHOD} 返回结构与契约不符：data 必须是对象列表",
            service=SERVICE,
            method=METHOD,
            outcome="unknown",
        )

    rows = [normalize_word(item) for item in data]
    rows.sort(key=lambda row: row.get("pv") or 0, reverse=True)
    if limit and limit > 0:
        rows = rows[:limit]
    return rows


async def plan(
    transport: BaiduTransport,
    seeds: list[str],
    *,
    limit_per_seed: int = 0,
) -> dict[str, Any]:
    """多种子拓词，按 ``word`` 去重后按搜索量降序合并。

    每个种子的 ``returned`` / ``new`` 都记在 ``seeds`` 里——
    「某个种子一个词都没拓出来」是需要看到的信号，不该被合并结果掩盖。
    """
    cleaned = [str(seed).strip() for seed in seeds if str(seed).strip()]
    if not cleaned:
        raise ValueError("至少需要一个种子词")

    merged: dict[str, dict[str, Any]] = {}
    stats: list[dict[str, Any]] = []
    truncated = False

    for seed in cleaned:
        rows = await expand(transport, seed, limit=limit_per_seed)
        if limit_per_seed and len(rows) >= limit_per_seed:
            truncated = True
        fresh = 0
        for row in rows:
            word = row.get("word")
            if word and word not in merged:
                merged[word] = row
                fresh += 1
        stats.append({"seed": seed, "returned": len(rows), "new": fresh})

    words = sorted(merged.values(), key=lambda row: row.get("pv") or 0, reverse=True)
    return {
        "total": len(words),
        "words": words,
        "seeds": stats,
        "coverage": dict(COVERAGE),
        "truncated": truncated,
    }
