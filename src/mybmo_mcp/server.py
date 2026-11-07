"""MCP 服务入口：stdio 与 Streamable HTTP 两种传输。

``stdio`` 传输下 stdout 属于协议帧，任何 ``print`` 都会破坏会话，
所有日志必须走 stderr。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from typing import Annotated, Literal

import typer
from mcp.server import MCPServer

from mybmo_mcp import __version__

Transport = Literal["stdio", "streamable-http"]

_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_INSTRUCTIONS = (
    "百度营销投放助手。调用工具前先确认账户别名，账户只能从 list_accounts "
    "返回的列表中选择。任何写操作必须先调用 change_preview 生成预览，"
    "由用户确认后再凭 approval_id 调用 change_execute。"
)

app = typer.Typer(
    name="mybmo-mcp",
    help=(
        "百度营销开放平台的 MCP 服务。\n\n"
        "stdio：本地接入，由 MCP 宿主作为子进程拉起（默认）。\n\n"
        "streamable-http：公网接入，需 OAuth 2.1 鉴权，默认监听 127.0.0.1:8000/mcp。"
    ),
    add_completion=False,
    rich_markup_mode=None,
)


def build_server() -> MCPServer:
    """构造 MCP 服务实例。工具、资源与鉴权在后续阶段挂载。"""
    return MCPServer(name="mybmo-mcp", version=__version__, instructions=_INSTRUCTIONS)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=_LOG_LEVELS.get(level.lower(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    transport: Annotated[
        Transport,
        typer.Option("--transport", "-t", help="传输方式"),
    ] = "stdio",
    host: Annotated[
        str,
        typer.Option("--host", help="HTTP 监听地址"),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="HTTP 监听端口"),
    ] = 8000,
    log_level: Annotated[
        str,
        typer.Option("--log-level", help="日志级别：debug/info/warning/error"),
    ] = "info",
) -> None:
    """启动 MCP 服务。"""
    if ctx.invoked_subcommand is not None:
        return
    _configure_logging(log_level)
    server = build_server()
    if transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport="streamable-http", host=host, port=port)


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。返回进程退出码。"""
    try:
        app(args=list(argv) if argv is not None else None)
    except KeyboardInterrupt:
        return 130
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 0
    return 0
