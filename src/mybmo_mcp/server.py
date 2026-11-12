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
from mybmo_mcp.auth.app_secrets import AppSecret
from mybmo_mcp.auth.binding import STATE_TTL
from mybmo_mcp.auth.callback import CallbackOutcome, complete_binding
from mybmo_mcp.baidu.errors import BaiduError
from mybmo_mcp.health import health_payload
from mybmo_mcp.subscribe import append_record, build_record, check_key, looks_like_baidu_key
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
    "账户只能从返回的列表里选择，不能自己编。列表为空时用 bind_account 让用户自己绑定"
    "一个百度账户（返回授权链接，用户自己在浏览器里完成授权）。"
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
    # 没有 outcome 表示「绑定入口本身没配好」，是服务端缺配置，不是请求有问题：503
    status = outcome.status_code if outcome is not None else 503
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

    secret = get_runtime().settings.mybmo_baidu_subscribe_secret.strip()
    if secret and not looks_like_baidu_key(secret):
        _logger.warning(
            "MYBMO_BAIDU_SUBSCRIBE_SECRET 不符合百度要求的格式（32 位、仅小写字母与数字），"
            "控制台会拒绝保存：len=%s",
            len(secret),
        )

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
    # 启动自检：pg 模式下每个租户都必须绑了 client_id，否则请求全 403 像"鉴权坏了"
    get_runtime().preflight()
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


class _StripQueryFilter(logging.Filter):
    """把访问日志里的 query string 抹掉。

    为什么必须有它：``/oauth/baidu/callback`` 的授权码就在 query 里，
    而 uvicorn 的 access log 用 ``get_path_with_query_string()`` 原样打印
    （2026-11-12 真实绑定实测：整串 ``authCode`` 进了 docker logs）。
    回调路由自己只记状态码与文案，这里补上被 access log 漏出去的那一份。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            target = args[2]
            if isinstance(target, str) and "?" in target:
                record.args = (args[0], args[1], target.split("?", 1)[0], *args[3:])
        return True


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=_LOG_LEVELS.get(level.lower(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # 失败关闭式的兜底：即便 access log 开着，也不让授权码进日志
    logging.getLogger("uvicorn.access").addFilter(_StripQueryFilter())


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
        typer.Option(
            "--tenant",
            help="租户 ID。多租户下必填——回调只能靠 state 里的 tenant 知道落哪个租户",
        ),
    ] = "",
    app_id: Annotated[
        str,
        typer.Option(
            "--app-id",
            help="用哪个应用授权（先用 `mybmo-mcp app secret set <appId>` 登记密钥）",
        ),
    ] = "",
) -> None:
    """生成百度授权链接。

    本命令**不等待授权结果**：它只签发一次性 state 并把链接打出来。授权完成后百度会跳回
    ``MYBMO_BAIDU_CALLBACK_URL``，由正在运行的服务（``-t streamable-http``）落库。
    也就是说 state 存在数据目录里，跨进程共享，绑定命令本身可以随时退出。
    """
    runtime = get_runtime()
    if not subject:
        typer.echo(
            "错误：必须用 --subject 指定该账户归属于谁的 sub，否则绑出来的账户没人有权使用。",
            err=True,
        )
        raise typer.Exit(code=2)

    # tenant 是 state 里唯一能证明"这次绑定属于哪个租户"的字段，多租户下不能猜。
    tenant = tenant.strip()
    if not tenant:
        typer.echo(
            "错误：多租户模式必须用 --tenant 指定租户（回调阶段只认 state 里的租户，"
            "留空会导致绑定无法落库）。先用 `mybmo-mcp tenant list` 查租户。",
            err=True,
        )
        raise typer.Exit(code=2)
    if not app_id:
        typer.echo(
            "错误：必须用 --app-id 指定用哪个应用授权（先 `mybmo-mcp app secret set <appId>`）",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        manager = runtime.binding
    except BaiduError as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise typer.Exit(code=2) from exc

    url, state = manager.authorize_url(
        subject=subject, tenant=tenant, alias=alias, app_id=app_id
    )
    minutes = int(STATE_TTL // 60)
    callback = runtime.settings.mybmo_baidu_callback_url
    typer.echo(
        f"账户别名：{state.alias}（归属 sub={state.subject} tenant={state.tenant} "
        f"app={state.app_id}）"
    )
    typer.echo(f"授权链接（{minutes} 分钟内有效，state 一次性）：")
    typer.echo("")
    typer.echo(url)
    typer.echo("")
    typer.echo("下一步：")
    typer.echo("  1. 在浏览器打开上面的链接，用要绑定的百度账户登录并授权")
    typer.echo(f"  2. 百度会跳回 {callback}，由运行中的服务完成落库（页面会显示结果）")
    typer.echo("  3. 跑 list_accounts 确认该别名可见")

    typer.echo(
        "  容器里执行：docker compose exec mcp mybmo-mcp auth bind --alias "
        f"{state.alias} --subject {state.subject} --tenant {state.tenant}"
    )


@auth_app.command("unbind")
def auth_unbind(
    alias: Annotated[str, typer.Argument(help="要解绑的账户别名")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """解绑账户：删凭据 + 注销归属与分享。

    换应用时先解绑再绑定。**不会**动审计日志——审计要能回放当时的调用。
    """
    runtime = get_runtime()
    if not yes:
        typer.confirm(f"确认解绑 {alias}？与它相关的分享会一并失效", abort=True)
    runtime.store.delete(alias)
    removed = runtime.tenancy.unregister_account(alias)
    typer.echo(f"凭据已删除：{alias}" + ("" if removed else "（授权表里本来就没有登记）"))


app_app = typer.Typer(
    name="app",
    help="应用密钥（appId / secretKey）登记。密钥只在服务器上隐藏输入，不经 MCP 工具与对话。",
    add_completion=False,
    rich_markup_mode=None,
)
secret_app = typer.Typer(
    name="secret", help="应用密钥的登记与查看（只看是否已登记，不回显密钥）。",
    add_completion=False, rich_markup_mode=None,
)


@secret_app.command("set")
def app_secret_set(
    app_id: Annotated[str, typer.Argument(help="百度开发者中心的应用 AppID")],
    scope: Annotated[str, typer.Option("--scope", help="授权 scope（应用级参数，可留空）")] = "",
    note: Annotated[str, typer.Option("--note", help="备注，便于区分多个应用")] = "",
    secret: Annotated[
        str,
        typer.Option(
            "--secret",
            help="不推荐：命令行会进 shell history。留空则交互式隐藏输入",
        ),
    ] = "",
) -> None:
    """登记（或更新）一个应用的 secretKey。"""
    runtime = get_runtime()
    value = secret.strip() or typer.prompt(
        "secretKey（输入不显示）", hide_input=True, confirmation_prompt=True
    )
    if not value.strip():
        typer.echo("错误：secretKey 不能为空", err=True)
        raise typer.Exit(code=2)
    runtime.app_secrets.save(
        AppSecret(app_id=app_id, secret_key=value.strip(), scope=scope, note=note)
    )
    typer.echo(f"已登记应用 {app_id}（密钥不回显）。下一步：")
    typer.echo(f"  mybmo-mcp auth bind --alias <别名> --app-id {app_id} --subject <sub>")


@secret_app.command("list")
def app_secret_list() -> None:
    """列出已登记的应用。**不显示密钥**。"""
    runtime = get_runtime()
    apps = runtime.app_secrets.list()
    if not apps:
        typer.echo("还没有登记任何应用。用 `mybmo-mcp app secret set <appId>` 登记。")
        return
    for app in apps:
        scope = f" scope={app.scope}" if app.scope else ""
        note = f"  # {app.note}" if app.note else ""
        typer.echo(f"{app.app_id}{scope}{note}")


@secret_app.command("remove")
def app_secret_remove(
    app_id: Annotated[str, typer.Argument(help="要删除的应用 AppID")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """删除应用密钥登记。已绑定的账户不受影响（它们各自存了密钥副本）。"""
    runtime = get_runtime()
    if not yes:
        typer.confirm(f"确认删除应用 {app_id} 的密钥登记？", abort=True)
    runtime.app_secrets.delete(app_id)
    typer.echo(f"已删除应用 {app_id}")


app_app.add_typer(secret_app, name="secret")
tenant_app = typer.Typer(
    name="tenant",
    help="租户管理。一个租户对应一个 Casdoor client_id；首次见到未知 client_id 会自动开户。",
    add_completion=False,
    rich_markup_mode=None,
)


@tenant_app.command("list")
def tenant_list() -> None:
    """列出租户（含未绑定 client_id 的）。"""
    runtime = get_runtime()
    rows = runtime.tenant_map.list()
    if not rows:
        typer.echo("还没有任何租户。首次带令牌的请求会自动开户。")
        return
    for row in rows:
        client = row.client_id or "(未绑定 client_id)"
        typer.echo(
            f"{row.id}  {row.name}  client_id={client}  users={row.users} accounts={row.accounts}"
        )


@tenant_app.command("set-client")
def tenant_set_client(
    tenant: Annotated[str, typer.Argument(help="租户 ID")],
    client_id: Annotated[str, typer.Argument(help="Casdoor 应用的 client_id")],
) -> None:
    """给租户绑定/修复 client_id。

    自动开户之外的唯一入口：存量租户（如 t_default）上线时要跑一次，
    或者把某个租户换到另一个 Casdoor 应用上。
    """
    runtime = get_runtime()
    runtime.tenant_map.set_client(tenant, client_id)
    typer.echo(f"已绑定：{tenant} ← {client_id}")


@tenant_app.command("rename")
def tenant_rename(
    tenant: Annotated[str, typer.Argument(help="租户 ID")],
    name: Annotated[str, typer.Argument(help="新的租户名（自动开户给的是 租户-xxxx 短码）")],
) -> None:
    """改租户名。只是展示用，不影响任何判定。"""
    runtime = get_runtime()
    runtime.tenant_map.rename(tenant, name)
    typer.echo(f"已改名：{tenant} → {name}")


app.add_typer(auth_app, name="auth")
app.add_typer(tenant_app, name="tenant")
app.add_typer(app_app, name="app")


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
