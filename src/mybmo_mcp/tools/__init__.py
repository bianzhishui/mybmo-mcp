"""MCP 工具层。

按工具域拆分为 accounts / keyword / report / entities / discover / write 等模块。
约定：工具函数只做参数校验与结果渲染，业务逻辑落在 ``mybmo_mcp.baidu.services``。

每个模块暴露 ``register(mcp)``，由 ``server.build_server()`` 统一挂载——
这样工具不需要导入 server，避免循环依赖。
"""

from __future__ import annotations

from mcp.server import MCPServer


def register_all(mcp: MCPServer) -> None:
    from mybmo_mcp.tools import accounts, discover, keyword

    for module in (accounts, keyword, discover):
        module.register(mcp)
