"""MCP 工具层。

六个常驻工具，分两类：

- ``accounts``：``list_accounts`` / ``auth_status`` / ``bind_account``，
  本项目自身能力；前两个不联网，``bind_account`` 只生成授权链接，用户自己在浏览器完成
- ``capability``：``search_capabilities`` / ``get_capability_schema`` /
  ``execute_capability``，能力网关三件套

每个模块暴露 ``register(mcp)``，由 ``server.build_server()`` 统一挂载——
工具不导入 server，避免循环依赖。

**工具层不做业务编排，也不做写操作拦截。** 写与不写由调用方（skill 工作流）决定。
"""

from __future__ import annotations

from mcp.server import MCPServer


def register_all(mcp: MCPServer) -> None:
    from mybmo_mcp.tools import accounts, capability

    accounts.register(mcp)
    capability.register(mcp)
