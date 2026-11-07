"""MCP 工具层。

按工具域拆分为 accounts / keyword / report / entities / discover / write 等模块。
约定：工具函数只做参数校验与结果渲染，业务逻辑落在 ``mybmo_mcp.baidu.services``。
"""
