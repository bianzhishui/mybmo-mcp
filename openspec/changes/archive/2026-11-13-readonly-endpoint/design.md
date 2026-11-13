# 设计：只读 MCP 入口

## 决策一览

| # | 决策 | 备选与否决原因 |
|---|---|---|
| D1 | **起第二个 `MCPServer` 实例** | 同一实例按请求路径分流：要先在协议层按路径过滤 `tools/list`，等于在工具层做策略，违反 ADR 0003；且 SDK 的 session manager 一个实例一条路径 |
| D2 | **只读入口是「装配差异」，不是策略层** | 在 `execute_capability` 里按角色/租户判断：那才是通用拦截，会引出"谁配规则、规则存哪"的一整套东西（另立变更） |
| D3 | **白名单式判定**：`mutating` 不是 `"false"` 就拒 | 黑名单（只拦 `"true"`）：`unknown` 就漏过去了。`unknown` 按写处理与 `required_permission()` 现有语义一致 |
| D4 | 守卫**返回 `fail(...)` 信封，不抛 `ToolError`** | 抛异常：与本项目"工具层统一结构化失败、不把栈甩给客户端"的约定冲突（`tools/_result.py` 开头即写明），且客户端无法区分"入口拒绝"与"工具崩了" |
| D5 | 拒绝记 **`state=denied`**，不新增 `forbidden` | 新增枚举要改 `002_audit_log.sql` 的 `CHECK` + 一次迁移，而 `denied` 语义正好是"权限/入口层面拒绝" |
| D6 | **同端口两条路由**（父 Starlette，**Route 直接复用，不用 Mount**），自己起 uvicorn | 独立端口/进程：反代、健康检查、`deploy/docker-compose.yml` 都要各来一套。用 `Mount`：会把子应用整包挂过去（连带它的 well-known/healthz），且 Starlette 的 `redirect_slashes` 让 `POST /mcp/readonly` 先吃一个 307（已实测） |
| D7 | 只读实例**换一份 instructions** | 复用现有文案：里面明确让模型"列表为空时用 `bind_account`"，而只读入口没有这个工具，模型会反复撞墙或判定服务故障 |
| D8 | 只读实例**不挂** `/healthz`、百度回调、订阅路由 | 重复挂会让人以为 `/mcp/readonly/healthz` 是另一个健康检查；探活永远看 `/healthz` 一个口径 |
| D9 | **开关关闭时仍走原来的 `MCPServer.run(transport=...)`** | 统一改成"总是自己组装 app"：装配路径变了，R4"逐字节一致"就不再是结构保证，只靠测试兜 |
| D10 | 子应用里**只取那一条 streamable Route**，其余路由丢掉 | 整包挂过去：子应用的 `/.well-known/...`、`/healthz`、回调路由会在 `/mcp/readonly` 前缀下再出现一份，语义混乱 |
| D11 | 只读实例**共用主机级 OAuth 元数据** | 自建一份 `resource_server_url`：`/.well-known/oauth-protected-resource` 会按路径分裂，客户端发现流程要改，收益为零 |
| D12 | 不做按用户/租户的工具裁剪、不收窄 `query_audit` | 需求方明确"另一个话题"；`query_audit` 已按用户隔离，收窄会破坏"查自己全部调用"的既有语义 |

## 代码形状

```python
# server.py
_READONLY_INSTRUCTIONS = (
    "百度营销投放能力网关（**只读入口**）。这里只有读能力：execute_capability "
    "遇到 mutating 不是 false 的能力会直接拒绝，请改用全量端点。"
    "账户用 list_accounts 确认别名；检索用 search_capabilities，"
    "取参数用 get_capability_schema。本入口不提供 bind_account / share_account / "
    "revoke_share。"
)

def build_server(*, with_auth: bool = True, readonly: bool = False) -> MCPServer:
    ...
    server = MCPServer(
        name="mybmo-mcp-readonly" if readonly else "mybmo-mcp",
        version=__version__,
        instructions=_READONLY_INSTRUCTIONS if readonly else _INSTRUCTIONS,
        **kwargs,
    )
    register_all(server, readonly=readonly)
    if not readonly:                    # D8：探活/回调/订阅只挂一次
        _register_health(server)
        _register_baidu_callback(server)
        _register_baidu_subscribe(server)
    return server
```

> 两个实例会各跑一次 `get_runtime().preflight()`（配置自检、无副作用）；
> 开关打开时 `_root` 不要先 `build_server()` 再交给 `_http_app()` 重复构造——
> 把构造收进 `_http_app()`，`_root` 只决定走哪条分支。

```python
# 装配：把两个子应用收到一个父 app 里
#
# **两个已实测的坑**（2026-11-13 本机跑通）：
# 1) StreamableHTTP 的 session manager 只在**应用自己的 lifespan** 里启动，
#    Starlette 不会运行 Mount 进去的子应用 lifespan —— 必须自己把两个
#    session_manager.run() 包进父 app 的 lifespan，否则请求直接抛
#    "Task group is not initialized"。
# 2) 用 Mount 会先吃一个 307（redirect_slashes），POST 被重定向；
#    改成把子应用那条 Route 的 endpoint 直接挂到父 app 的完整路径上，零重定向。

def _http_app(*, readonly_path: str, host: str) -> Starlette:
    full = build_server()
    full_app = full.streamable_http_app(streamable_http_path="/mcp", host=host)
    if not readonly_path:
        return full_app

    readonly = build_server(readonly=True)
    child = readonly.streamable_http_app(streamable_http_path="/", host=host)
    # 只保留 streamable 端点那一条；SDK 升级把路由结构改了就在启动期炸掉，
    # 而不是静默把别的东西挂到 /mcp/readonly 下（失败关闭）
    endpoint = [r for r in child.routes if getattr(r, "path", None) == "/"]
    if len(endpoint) != 1:
        raise RuntimeError(
            f"只读入口装配失败：期望子应用恰好 1 条 streamable 路由，实际 {len(endpoint)} 条"
        )

    @asynccontextmanager
    async def lifespan(_: Starlette):
        async with full.session_manager.run():
            async with readonly.session_manager.run():
                yield

    return Starlette(
        routes=[Route(readonly_path, endpoint=endpoint[0].endpoint), *full_app.routes],
        lifespan=lifespan,
    )
```

> 实测记录（2026-11-13，本机 `httpx.ASGITransport` + 线上 pandabase-dev01 真实令牌）：
> `/mcp` 与 `/mcp/readonly` 都能 `initialize` 成功（200 + `mcp-session-id`）、
> `tools/list` 各自返回完整工具集；带鉴权时两者都正确回 `401 + invalid_token`。
> 测试里要注意两点：ASGITransport **不跑 lifespan**，得手动 `async with lifespan(app)`；
> 传 `host="127.0.0.1"` 会开 DNS rebinding 保护，测试的 `base_url` 必须带端口。

```python
# _root 里的分支（D9：关闭时不碰原路径）
settings = get_runtime().settings
if transport == "stdio":
    build_server().run(transport="stdio")
elif not settings.mybmo_mcp_readonly:
    build_server().run(transport="streamable-http", host=host, port=port)
else:
    uvicorn.run(
        _http_app(readonly_path=settings.mybmo_mcp_readonly_path, host=host),
        host=host, port=port, log_level=log_level,
    )
```

```python
# tools/capability.py —— 守卫位置：拿到 cap 之后、normalize 之前
cap = registry.get(capability)
if cap is None:
    return _not_found(registry, capability)

if readonly:
    # 拒绝也要留痕：能不能写是入口决定的，不是身份决定的，
    # 事后要知道"谁在只读入口上试图写什么"。
    return fail(
        f"readonly endpoint：能力 {cap.key!r} 的 mutating={cap.mutating}，"
        "本入口只允许只读能力，已拒绝。请改用全量端点 /mcp 执行写操作。",
        hint="只读入口的 execute_capability 仅接受 mutating=false 的能力",
        extra={"capability": cap.key, "mutating": cap.mutating, "endpoint": "readonly"},
    )
```

审计那条 `denied` 记录在守卫点写一次（`principal` 已可得，`payload` 尚未归一化就给原样入参），
字段与现有 `_audit()` 一致，`state=STATE_DENIED`、`error` 用同一句文本。

## 鉴权为什么能直接复用

`RequireAuthMiddleware` 的 `resource_metadata_url` 由 `settings.auth.resource_server_url`
推导，而它是**主机级**的（`MYBMO_RESOURCE_URL=https://baiduads.pandabase.top`，无路径），
所以两个入口共用同一份 `/.well-known/oauth-protected-resource`：
`aud` 白名单、`MYBMO_OIDC_ORGS`、tenancy 全部原封不动，不需要新身份、新授权流程。
子应用里那份 well-known 会被 `Mount` 前缀成 `/mcp/readonly/.well-known/...` 而不是重复注册，
D10 的过滤也会把它摘掉。

## 已知副作用：全系统共 **2 次**授权（不是每成员一次）

`mcp-remote` 的令牌缓存键是
`md5([serverUrl, resource, authorizeParams, headers, clientMetadataUrl, tokenEndpoint].join("|"))`，
落在 `~/.mcp-auth/mcp-remote-v1/{hash}_tokens.json`，一个 hash 一份 token 加一份刷新租约
（`src/lib/utils.ts:getServerUrlHash`、`src/lib/mcp-auth-config.ts`）。由此：

- **同一 URL 的所有成员共用一份 `tokens.json`**：第一个启动的做授权，其余直接复用，
  还有专门的 sibling-token 交接（*"a handover costs no browser tab and no authorization code"*）。
- `/mcp` 与 `/mcp/readonly` 是两个 hash ⇒ **全系统 2 次授权**，之后所有成员共用；不是每个成员各一次。
- 缓存按 OS 用户存放：多机/多用户部署时是"每人每 URL 一次"。

`MYBMO_RESOURCE_URL` 已显式配置（主机级、无路径），不参与 hash，所以两处入口的 OAuth 元数据是同一份。

**想压到 1 次是不可行的（用 `mcp-remote` 时）**：hash 的输入集就是"URL + headers + params + resource"，
任何能让服务端区分只读角色的信号都在这套输入里——用了就多一个 hash、多一次授权；
反过来"同一 URL + 同一 token"意味着服务端在连接层无法区分调用方，工具面只能一样，
结构保证就退化回按身份/按头裁剪（也正是 ADR 0011 否掉的方向）。真要做到 1 次，
只能去掉 `mcp-remote`、改 DSH 直连 `streamable-http` + 自带 token 刷新（另立变更）。
**本次决定：保留路径、接受 2 次一次性授权。**

## 测试怎么测

| 层 | 手段 |
|---|---|
| 工具面 | `build_server(readonly=True)._tool_manager.list_tools()` → 恰好 7 个，且不含三个写工具 |
| 守卫 | 直接 `await execute_capability(...)`（fake runtime），断 `ok is False`、文本含 `readonly endpoint` / `mutating=true` / 建议全量端点 |
| `unknown` | `PreAuditService.materialPreAudit`（能力表里**唯一**的 `unknown`） |
| 审计 | fake audit sink 收一场 `state == "denied"` |
| 路由 | `_http_app(readonly_path="/mcp/readonly")` 的 `app.routes` 里同时有 `/mcp` 与 `/mcp/readonly`；`readonly_path=""` 时与现在的 app 一致（`Route` 复用、无 `Mount`、无 307） |
| 端到端 | 用 `httpx.ASGITransport` + 手动 `async with lifespan(app)` 跑一次 `initialize` + `tools/list`，两入口各拿 10 / 7 |
| 一致性 | 开关关闭时 `_root` 必须调到 `MCPServer.run(transport="streamable-http")`（monkeypatch 断言），这是 R4 的结构保证 |

## 风险

| 风险 | 应对 |
|---|---|
| SDK 升级改变 `streamable_http_app()` 的路由结构，或改掉 `session_manager` 属性 | 装配期断言路由数（失败关闭）+ 端到端回归测试（同时覆盖 lifespan 是否真的启动了 session manager） |
| 两条入口的 instructions/工具集漂移 | 工具面用同一份 `register_all(..., readonly=)`；测试双向断言 10 / 7 |
| 有人顺着只读入口往工具层加通用白名单 | ADR 0011 写死"这是装配差异"，review 时以此为据 |
| 只读入口挡住 `materialPreAudit` | 该能力在 `sms` 线、本项目不承诺；若将来电商线要它，先实测再把它标成 `mutating=false`，而不是在入口开洞 |
