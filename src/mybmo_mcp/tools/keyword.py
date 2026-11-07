"""关键词工具。"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from mybmo_mcp.baidu.services import keyword as keyword_service
from mybmo_mcp.tools._result import guarded
from mybmo_mcp.tools.runtime import get_runtime


async def keyword_plan(
    seeds: list[str],
    limit_per_seed: int = 0,
    account: str | None = None,
) -> dict[str, Any]:
    """关键词规划师拓词：给种子词，返回词表、日均搜索量、竞争度、推荐出价。

    结果里的 coverage 会说明「这是单次响应，接口上限未实测」，
    汇报时请把这句话带上，不要把单次结果说成全量。
    """

    async def _run() -> dict[str, Any]:
        transport = get_runtime().transport_for(account)
        return await keyword_service.plan(transport, seeds, limit_per_seed=limit_per_seed)

    return await guarded(_run)


def register(mcp: MCPServer) -> None:
    mcp.tool()(keyword_plan)
