"""MCP 工具层。

九个常驻工具（另有 ``query_audit`` 查调用审计），分两类：

- ``accounts``：``list_accounts`` / ``auth_status`` / ``bind_account``，
  本项目自身能力；前两个不联网，``bind_account`` 只生成授权链接，用户自己在浏览器完成
- ``capability``：``search_capabilities`` / ``get_capability_schema`` /
  ``execute_capability``，能力网关三件套

每个模块暴露 ``register(mcp)``，由 ``server.build_server()`` 统一挂载——
工具不导入 server，避免循环依赖。

**工具层不做业务编排，也不做写操作拦截。** 写与不写由调用方（skill 工作流）决定。

唯一的例外是**只读入口**（``/mcp/readonly``，ADR 0011）：那里 ``execute_capability``
带一道 ``mutating`` 白名单守卫，且三个改授权的账户工具根本不会注册。
它判的是能力自身的读写属性、与"谁在调"无关，所以仍是装配差异而不是策略层。
"""

from __future__ import annotations

from mcp.server import MCPServer


def register_all(mcp: MCPServer, *, readonly: bool = False) -> None:
    """挂载全部工具。

    ``readonly=True`` 时只挂**只读**那一份（7 个）：账户类去掉
    ``bind_account`` / ``share_account`` / ``revoke_share``，
    ``execute_capability`` 换成带 ``mutating`` 白名单守卫的实现。
    这是**装配层面**的差异，不是工具层的通用拦截——不用身份去判"谁能写"，
    那属于授权层（ADR 0003 / 0011）。
    """
    from mybmo_mcp.tools import accounts, audit, capability

    accounts.register(mcp, readonly=readonly)
    capability.register(mcp, readonly=readonly)
    audit.register(mcp)
