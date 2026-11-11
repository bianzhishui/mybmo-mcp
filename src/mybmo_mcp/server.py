"""MCP 服务入口：stdio 与 Streamable HTTP 两种传输。

``stdio`` 传输下 stdout 属于协议帧，任何 ``print`` 都会破坏会话，
所有日志必须走 stderr。
"""

from __future__ import annotations

import html
import json
import logging
import sys
from collections.abc import Sequence
from typing import Annotated, Any, Literal

import typer
from mcp.server import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from mybmo_mcp import __version__
from mybmo_mcp.auth.binding import STATE_TTL
from mybmo_mcp.auth.callback import CallbackOutcome, complete_binding
from mybmo_mcp.auth.tenancy import MODE_LOCAL, Principal
from mybmo_mcp.baidu.errors import BaiduError
from mybmo_mcp.health import health_payload
from mybmo_mcp.subscribe import append_record, build_record, check_key
from mybmo_mcp.tools import register_all
from mybmo_mcp.tools.runtime import get_runtime

Transport = Literal["stdio", "streamable-http"]

#: 百度 OAuth 的授权回调路径。与 ``MYBMO_BAIDU_CALLBACK_URL`` 的路径部分必须一致，
#: 且**根域**要与 dev2 应用「安全设置」里登记的保持一致（百度按根域比对）。
#: 这是一条免鉴权路由：跳转过来的是浏览器，带不了 Bearer 令牌。
BAIDU_CALLBACK_PATH = "/oauth/baidu/callback"

#: 百度订阅（事件推送）的接收路径。与授权回调是两套机制，别互相填。
#: 同样免 Token 鉴权，靠**共享密钥**把关；密钥没配就 503（失败关闭）。
BAIDU_SUBSCRIBE_PATH = "/oauth/baidu/subscribe"

_logger = logging.getLogger(__name__)

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
        "对外暴露前必须先把鉴权加上，见 docs/auth-contract.md。"
    ),
    add_completion=False,
    rich_markup_mode=None,
)


def _register_health(server: MCPServer) -> None:
    """``/healthz`` 走 SDK 的自定义路由，不参与 MCP 协议、不需要鉴权。"""

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse(health_payload())


def _callback_page(
    outcome: CallbackOutcome | None, *, title: str, message: str
) -> HTMLResponse:
    """给浏览器看的结果页。**只回显 alias 与账户中文名，不回显任何令牌片段。**"""
    rows = ""
    if outcome is not None:
        labels = {"alias": "账户别名", "user_name": "账户中文名", "status": "状态"}
        for key, label in labels.items():
            value = outcome.data.get(key)
            if value:
                rows += f"<tr><th>{label}</th><td>{html.escape(str(value))}</td></tr>"
        if outcome.hints:
            items = "".join(f"<li>{html.escape(hint)}</li>" for hint in outcome.hints)
            rows += f'<tr><th>注意</th><td><ul>{items}</ul></td></tr>'
    body = (
        "<!doctype html><meta charset='utf-8'>"
        "<title>mybmo-mcp · 百度账户绑定</title>"
        "<style>body{font:15px/1.6 -apple-system,system-ui,sans-serif;margin:3rem auto;"
        "max-width:40rem;padding:0 1rem;color:#222}"
        "table{border-collapse:collapse;margin-top:1rem}"
        "th,td{text-align:left;padding:.35rem .8rem;border-bottom:1px solid #eee;"
        "vertical-align:top}th{color:#666;font-weight:500;white-space:nowrap}"
        "ul{margin:0;padding-left:1.2rem}.hint{color:#888;margin-top:1.5rem}</style>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
        f"<table>{rows}</table>"
        "<p class='hint'>这一页只用于人工确认；凭据已写入服务端加密存储，不会出现在浏览器里。</p>"
    )
    status = outcome.status_code if outcome is not None else 500
    return HTMLResponse(body, status_code=status)


def _register_baidu_callback(server: MCPServer) -> None:
    """挂载百度授权回调。

    这条路由**免 Token 鉴权**（百度跳转的是浏览器），所以它自己的安全性靠三件事：
    一次性 state、15 分钟 TTL、以及"授权码只能换一次"的上游事实。
    日志只记状态码与文案，绝不记查询参数——里面有授权码。
    """

    @server.custom_route(BAIDU_CALLBACK_PATH, methods=["GET"], include_in_schema=False)
    async def baidu_callback(request: Request) -> HTMLResponse:
        runtime = get_runtime()
        try:
            manager = runtime.binding
        except BaiduError as exc:
            _logger.error("百度授权回调不可用：绑定入口未配置")
            return _callback_page(None, title="绑定入口未配置", message=str(exc))

        outcome = await complete_binding(manager, request.query_params)
        _logger.info("百度授权回调：status=%s %s", outcome.status_code, outcome.message)
        title = "绑定成功" if outcome.ok else "绑定未完成"
        return _callback_page(outcome, title=title, message=outcome.message)


def _register_baidu_subscribe(server: MCPServer) -> None:
    """挂载百度订阅（事件推送）接收端：验密钥 → 打印并落盘 → 回 200。

    免 Token 鉴权（百度服务端不会带我们的 Bearer），所以三条铁律见
    :mod:`mybmo_mcp.subscribe`：不配密钥 503、验不过不落盘、落盘前脱敏。
    这里**故意不解析成业务动作**——能力层不编排（ADR 0003），
    推送只当样本采集，攒够真实报文再写解析器。
    """

    @server.custom_route(
        BAIDU_SUBSCRIBE_PATH,
        methods=["GET", "POST", "PUT"],
        include_in_schema=False,
    )
    async def baidu_subscribe(request: Request) -> JSONResponse:
        secret = get_runtime().settings.mybmo_baidu_subscribe_secret.strip()
        if not secret:
            _logger.error("收到订阅推送但未配置 MYBMO_BAIDU_SUBSCRIBE_SECRET，拒绝接收")
            return JSONResponse(
                {"ok": False, "error": "subscribe endpoint not configured"}, status_code=503
            )

        ok, key_source = check_key(secret, request.headers, request.query_params)
        client = request.client.host if request.client is not None else ""
        if not ok:
            # 只记一行，不落 body——否则扫描器就能把磁盘灌满
            _logger.warning(
                "订阅推送验签失败：client=%s method=%s 命中来源=%s",
                client,
                request.method,
                key_source or "无",
            )
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

        body = await request.body()
        record = build_record(
            method=request.method,
            path=request.url.path,
            headers=request.headers,
            params=request.query_params,
            body=body,
            secret=secret,
            key_source=key_source,
            client=client,
        )
        try:
            path = append_record(get_runtime().settings.mybmo_data_dir, record)
        except OSError as exc:
            # 落盘失败不影响回 200：百度重推一遍也不会更好，先在日志里留证据
            _logger.error("订阅推送落盘失败（%s），仅打印", type(exc).__name__)
            path = None

        _logger.info(
            "收到订阅推送（%s）：%s",
            key_source,
            json.dumps(record, ensure_ascii=False, indent=2),
        )
        return JSONResponse({"ok": True, "stored": str(path) if path else None}, status_code=200)


def build_server(*, with_auth: bool = True) -> MCPServer:
    """构造 MCP 服务实例并挂载工具。

    ``with_auth`` 只对 streamable-http 有意义：stdio 下没有 HTTP 层，
    也就没有 bearer 令牌可校验。装配由 :func:`_auth_kwargs` 决定，
    local 模式返回空字典（不装中间件），oidc 模式返回 ``auth`` + ``token_verifier``。
    """
    kwargs = _auth_kwargs() if with_auth else {}
    server = MCPServer(
        name="mybmo-mcp",
        version=__version__,
        instructions=_INSTRUCTIONS,
        **kwargs,
    )
    register_all(server)
    _register_health(server)
    _register_baidu_callback(server)
    _register_baidu_subscribe(server)
    return server


def _auth_kwargs() -> dict[str, Any]:
    """按配置决定是否挂载 OAuth Resource Server。

    local 模式下**故意不装鉴权**：stdio 没有 HTTP 层，加了反而让 SDK 期望 bearer 令牌。
    oidc 模式下 ``Runtime.auth_settings()`` 必然非空——缺配置会在构造 verifier 时抛错，
    而不是悄悄退化成"不鉴权"。后者是最坏的结果：看起来配好了，实际谁都能进。
    """
    runtime = get_runtime()
    settings = runtime.auth_settings()
    verifier = runtime.verifier
    if settings is None or verifier is None:
        return {}
    return {"auth": settings, "token_verifier": verifier}


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


auth_app = typer.Typer(
    name="auth",
    help="百度账户授权。绑定完成后凭据写入服务端加密存储。",
    add_completion=False,
    rich_markup_mode=None,
)


@auth_app.command("bind")
def auth_bind(
    alias: Annotated[
        str,
        typer.Option("--alias", "-a", help="账户别名。模型只能用别名选账户，绑完不能再改"),
    ],
    subject: Annotated[
        str,
        typer.Option(
            "--subject",
            "-s",
            help="身份提供方的 sub（oidc 模式必填：谁有权用这个账户）；local 模式默认本机用户",
        ),
    ] = "",
    tenant: Annotated[
        str,
        typer.Option("--tenant", help="租户 ID；local 模式默认 local"),
    ] = "",
) -> None:
    """生成百度授权链接。

    本命令**不等待授权结果**：它只签发一次性 state 并把链接打出来。授权完成后百度会跳回
    ``MYBMO_BAIDU_CALLBACK_URL``，由正在运行的服务（``-t streamable-http``）落库。
    也就是说 state 存在数据目录里，跨进程共享，绑定命令本身可以随时退出。
    """
    runtime = get_runtime()
    local_mode = runtime.settings.mybmo_auth_mode == MODE_LOCAL
    if local_mode:
        local_admin = Principal.local_admin()
        subject = subject or local_admin.sub
        tenant = tenant or local_admin.tenant_id
    elif not subject:
        typer.echo(
            "错误：oidc 模式必须用 --subject 指定该账户归属于谁的 sub，"
            "否则绑出来的账户没人有权使用。",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        manager = runtime.binding
    except BaiduError as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise typer.Exit(code=2) from exc

    url, state = manager.authorize_url(subject=subject, tenant=tenant, alias=alias)
    minutes = int(STATE_TTL // 60)
    callback = runtime.settings.mybmo_baidu_callback_url
    typer.echo(f"账户别名：{state.alias}（归属 sub={state.subject} tenant={state.tenant}）")
    typer.echo(f"授权链接（{minutes} 分钟内有效，state 一次性）：")
    typer.echo("")
    typer.echo(url)
    typer.echo("")
    typer.echo("下一步：")
    typer.echo("  1. 在浏览器打开上面的链接，用要绑定的百度账户登录并授权")
    typer.echo(f"  2. 百度会跳回 {callback}，由运行中的服务完成落库（页面会显示结果）")
    typer.echo("  3. 跑 list_accounts 确认该别名可见")
    if not local_mode:
        typer.echo(f"  4. 在 tenancy.json 里给 sub={state.subject} 授予该账户的档位")
    typer.echo(
        "  容器里执行：docker compose exec mcp mybmo-mcp auth bind --alias "
        f"{state.alias}" + ("" if local_mode else f" --subject {state.subject}")
    )


app.add_typer(auth_app, name="auth")


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
