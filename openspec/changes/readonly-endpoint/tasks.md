# 任务：只读 MCP 入口 `/mcp/readonly`

> 勾选标准：能被 `tools/list`、一条测试或线上实测证明。
> 对应需求单 `docs/req-mybmo-mcp-readonly-endpoint.md` §六 的 8 条用例。

## 1. 配置

- [x] 1.1 `config.py`：`mybmo_mcp_readonly: bool = False`、`mybmo_mcp_readonly_path: str = "/mcp/readonly"`
- [x] 1.2 路径校验（启动期，失败关闭）：非空时必须以 `/` 开头；**不得等于 `/mcp`**，
      也不得是 `/mcp` 的超集里会遮住它的写法（`/mcp` 是 `/mcp/readonly` 的前缀，方向正确即可）
- [x] 1.3 `.env.example` 与 `deploy/.env.example` 补两项，注明"不设即完全关闭"

**出口**：`tests/test_cli.py` 或新增配置测试覆盖三条非法路径 + 默认值。

## 2. 工具面裁剪

- [x] 2.1 `tools/__init__.py`：`register_all(mcp, *, readonly: bool = False)`
- [x] 2.2 `tools/accounts.py`：`register(mcp, *, readonly=False)`，只读时跳过
      `bind_account` / `share_account` / `revoke_share`（保留 `list_shares`）
- [x] 2.3 `tools/capability.py`：`register(mcp, *, readonly=False)`，闭包把 `readonly` 传给 `execute_capability`

**出口**：`build_server(readonly=True)` → 恰好 7 个工具；`build_server()` 仍是 10 个（双向断言）。

## 3. 只读守卫

- [x] 3.1 守卫放在 `registry.get(cap)` 之后、`normalize_payload` 之前；
      判定 `required_permission(cap.mutating) != PERMISSION_READ` 即拒
- [x] 3.2 拒绝返回**统一失败信封**（`fail(...)`，不抛 `ToolError`），文本必须含
      `readonly endpoint`、能力 key、`mutating=<值>`、"请改用全量端点 /mcp"
- [x] 3.3 拒绝写一条审计：`state=STATE_DENIED`、同句 error、带 principal 与原始入参
- [x] 3.4 全量实例不受影响：同一能力在全量入口照常执行

**出口**：
- `ImageManageService.uploadImage`（`mutating=true`）→ `ok=false`，文本含 `mutating=true`
- `CampaignShoppingService.addCampaignShopping`（`mutating=true`）→ 同上
- `PreAuditService.materialPreAudit`（**唯一**的 `mutating=unknown`）→ 同样被拒
- `KRService.getKRByQuery`（`mutating=false`）→ 放行（用 fake runtime，不联网）
- fake audit sink 收到一场 `denied`

## 4. HTTP 装配

- [x] 4.1 `server.py`：`build_server(*, with_auth=True, readonly=False)`；
      只读实例名 `mybmo-mcp-readonly` + 只读版 instructions（不提 `bind_account`）
- [x] 4.2 只读实例不挂 `/healthz` / 百度回调 / 订阅路由
- [x] 4.3 `_http_app(*, readonly_path, host)`：父 Starlette；**把子应用那条 streamable `Route`
      的 endpoint 挂到完整路径**（不用 `Mount`，否则先吃一个 307）；只保留 1 条，数量不符启动期报错
- [x] 4.3b 父 app 的 `lifespan` 里 `async with full.session_manager.run(): async with readonly.session_manager.run():`
      ——**Starlette 不会跑子应用的 lifespan，不包就会 `Task group is not initialized`**
- [x] 4.4 `_root` 分支：stdio 不变；**开关关闭时仍调 `server.run(transport="streamable-http")`**（逐字节一致的结构保证）；
      开关打开时 `uvicorn.run(_http_app(...))`

**出口**：
- `_http_app(readonly_path="/mcp/readonly")` 的路由里同时有 `/mcp` 与 `/mcp/readonly`，且无 `Mount`
- `readonly_path=""` 时路由集合与现在一致
- monkeypatch 断言：开关关闭 → 走 `MCPServer.run`；打开 → 走 `uvicorn.run`
- 端到端：`httpx.ASGITransport` + 手动 `async with lifespan(app)`，两个入口各 `initialize`
  一次拿到 `mcp-session-id`，`tools/list` 分别 10 / 7（这是唯一能证明"session manager 真的起来了"的用例）

## 5. 验收用例（需求单 §六 逐条）

- [x] 5.1 只读入口 `tools/list` 恰好 7 个，不含三个写工具
- [x] 5.2 只读入口 `execute_capability("ImageManageService.uploadImage", …)` → 拒绝
- [x] 5.3 只读入口 `execute_capability("CampaignShoppingService.addCampaignShopping", …)` → 拒绝
- [x] 5.4 只读入口 `execute_capability("KRService.getKRByQuery", {…})` → 成功（fake transport）
- [x] 5.5 只读入口 `execute_capability(<mutating=unknown>)` → 拒绝
- [x] 5.6 只读入口 `search_capabilities` / `get_capability_schema` / `list_accounts` /
      `auth_status` / `list_shares` / `query_audit` → 全部成功
- [x] 5.7 全量入口回归：10 个工具、写能力可调
- [x] 5.8 `MYBMO_MCP_READONLY` 未设置 → `/mcp/readonly` 不在路由里（线上表现为 404）

**出口**：新增 `tests/test_readonly_endpoint.py`，上述 8 条全绿；既有 310 例不回归。

## 6. 文档

- [x] 6.1 新增 `docs/decisions/0011-readonly-endpoint.md`
- [x] 6.2 `docs/auth-contract.md`：端点表补 `/mcp/readonly`，写清"共用同一份 OAuth 元数据"
- [x] 6.3 `docs/architecture.md`：信任边界图里加第二个入口
- [x] 6.4 `README.md`：端点与两个新开关；说明 `mcp-remote` 按 URL 缓存令牌 ⇒ 两个入口各授权一次、全员复用
- [x] 6.5 `openspec/project.md` 变更状态表 + `openspec/changes/README.md` 当前表
- [x] 6.6 `docs/req-mybmo-mcp-readonly-endpoint.md` 顶部补一行"已立项：`changes/readonly-endpoint/`"

## 7. 上线与回滚

> **上线时抓到并已修的缺陷**：`MYBMO_MCP_READONLY=`（置空）会被 pydantic 判成非法
> 布尔值，Settings 构造抛 `ValidationError`，**服务直接起不来**——而"置空/删掉"正是
> 回滚动作。已加 before 校验器（空/0/false/no/off → 关闭）并补 10 例测试，
> commit `f747830`。教训：布尔开关的空值语义必须显式定义，不能让解析器替我们决定。

- [x] 7.1 `deploy/.env` 设 `MYBMO_MCP_READONLY=1`，重建容器
      （2026-11-13 已在 pandabase-dev01 完成：容器 healthy，日志有
      `只读入口已启用：/mcp/readonly` 与两行 `session manager started`；
      重建前把旧镜像 `mybmo-mcp:0.2.0` 另存为 `mybmo-mcp:0.2.0-prev-readonly`）
- [ ] 7.1b 客户端授权：部署机上依次连一次 `/mcp` 与 `/mcp/readonly` 完成两次浏览器授权
      （之后同 OS 用户的全部成员复用 `~/.mcp-auth/mcp-remote-v1/*_tokens.json`，无需再授权）
- [x] 7.2 反代：**无需改动**。`/opt/pandabase-infra/deploy/Caddyfile` 用的是兜底
      `handle { reverse_proxy }`，`/mcp/readonly` 已在转发范围内
      （实测公网 `POST /mcp/readonly` → 401，说明请求到了业务进程）
- [ ] 7.3 实测：`/mcp` 10 工具；`/mcp/readonly` 7 工具；只读入口调 `uploadImage` 被拒且审计有 `denied`
      （**待有令牌后执行**：`tools/list` 与 `execute_capability` 都要过鉴权，见 7.1b）
- [ ] 7.4 实测：`KRService.getKRByQuery` 在只读入口拿到拓词结果（真实账户）
- [x] 7.5 回滚：删 `MYBMO_MCP_READONLY` 即回到单入口，无需改镜像
      （2026-11-13 用同镜像 + 生产 env 起临时容器实测：`/mcp` → 401、`/mcp/readonly` → 404）
- [ ] 7.6 完成后按 `changes/README.md` 归档为 `archive/2026-MM-DD-readonly-endpoint/`
