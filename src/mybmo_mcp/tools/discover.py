"""接口发现工具。

``api_describe`` **不需要凭据**：没有账户、没有主密钥时也能用，
用来回答「这个接口叫什么、是不是写操作、走哪条产品线」。
这也是无凭据场景下唯一能给出实质帮助的工具。
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from mybmo_mcp.baidu.catalog import Catalog
from mybmo_mcp.tools._result import fail, ok


def api_describe(service: str = "", method: str = "", keyword: str = "") -> dict[str, Any]:
    """查百度营销接口目录（133 个服务 / 399 个接口）。

    两种用法：

    - 给 ``service`` + ``method``：返回是否写操作、产品线、所属能力域
    - 给 ``keyword``：按服务名或方法名模糊检索

    目录是提示不是白名单：未收录的接口仍可尝试，但只读透传会以目录为准做校验。
    """
    catalog = Catalog.load()

    if service and method:
        info = catalog.describe(service, method)
        if info is None:
            return fail(
                f"目录中没有 {service}/{method}",
                hint="用 api_describe(keyword=...) 模糊检索，或确认服务名与方法名拼写",
            )
        return ok(info)

    if keyword:
        hits = catalog.search(keyword)
        if not hits:
            return fail(
                f"没有匹配 {keyword!r} 的接口", hint="换更短的关键词，例如 kr、campaign、report"
            )
        return ok(hits)

    return fail(
        "需要 service + method，或 keyword",
        hint="例如 api_describe(service='KRService', method='getKRByQuery')",
    )


def register(mcp: MCPServer) -> None:
    mcp.tool()(api_describe)
