"""探活端点。

部署里的健康检查、监控告警、以及"服务到底起没起来"的排查都靠它，
所以它必须满足三条：**不依赖凭据、不泄漏账户信息、不能被阻塞**。

单独成文件是为了能被单测直接调用，不必先把整个 MCP 服务构造出来。
"""

from __future__ import annotations

from typing import Any

from mybmo_mcp import __version__
from mybmo_mcp.baidu.errors import CredentialError
from mybmo_mcp.tools.runtime import get_runtime


def health_payload() -> dict[str, Any]:
    """探活返回体。

    ``credentials=unconfigured`` 时服务仍算活着——能检索接口目录、能返回结构化错误，
    只是调不动百度。所以状态码保持 200，由监控按字段告警，而不是让容器被判死重启。
    """
    runtime = get_runtime()
    payload: dict[str, Any] = {
        "status": "ok",
        "version": __version__,
        "auth_mode": runtime.settings.mybmo_auth_mode,
    }

    try:
        stats = runtime.capabilities.stats
        payload["capabilities"] = stats["capabilities"]
        payload["described"] = stats["described"]
    except (OSError, ValueError):
        # 能力表读不出来是打包问题，值得暴露在探活里，但不该让进程判死
        payload["capabilities"] = None

    try:
        runtime.settings.master_key_bytes()
        payload["credentials"] = "configured"
    except CredentialError:
        payload["credentials"] = "unconfigured"

    return payload
