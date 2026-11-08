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
from starlette.requests import Request
from starlette.responses import JSONResponse

from mybmo_mcp import __version__
from mybmo_mcp.health import health_payload
from mybmo_mcp.tools import register_all

Transport = Literal["stdio", "streamable-http"]

_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_INSTRUCTIONS = (
    "百度营销投放能力网关。调用任何接口前，先用 list_accounts 确认账户别名——"
    "账户只能从返回的列表里选择，不能自己编。"
    "接口能力通过 search_capabilities 检索，用 get_capability_schema 取参数定义，"
    "再用 execute_capability 执行。本服务不判断操作是否安全，写操作前是否需要"
    "人工确认，由调用方自己的工作流决定。"
)

app = typer.Typer(
    name="mybmo-mcp",
    help=(
        "百度营销开放平台的 MCP 服务。\n\n"
        "stdio：本地接入，由 MCP 宿主作为子进程拉起（默认）。\n\n"
        "streamable-http：公网接入，默认 127.0.0.1:8000/mcp。"
        "对外暴露前必须先把鉴权加上，见 docs/operations.md。"
    ),
    add_completion=False,
    rich_markup_mode=None,
)


def _register_health(server: MCPServer) -> None:
    """``/healthz`` 走 SDK 的自定义路由，不参与 MCP 协议、不需要鉴权。"""

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse(health_payload())


def build_server() -> MCPServer:
    """构造 MCP 服务实例并挂载工具。"""
    server = MCPServer(name="mybmo-mcp", version=__version__, instructions=_INSTRUCTIONS)
    register_all(server)
    _register_health(server)
    return server


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
