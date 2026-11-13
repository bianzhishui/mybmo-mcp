# 变更：只读 MCP 入口 `/mcp/readonly`（readonly-endpoint）

> **状态**：✅ 已完成并上线（2026-11-13）。`MYBMO_MCP_READONLY=1` 正在 pandabase-dev01 运行，
> 8 条验收用例全部通过（含真实令牌与真实拓词）。需求来源：
> [`docs/req-mybmo-mcp-readonly-endpoint.md`](../../../../docs/req-mybmo-mcp-readonly-endpoint.md)
> （提出方 `mybmo-agents`，与 `mybmo-agents:docs/dsh-multi-agent-v1.md` §5.2 / §5.3 关联）。

## 为什么

`mybmo-agents` 的 8 角色链路要求"**所有 `mutating: true` 的能力只允许实施操作员调用**"，
但执行环境（DeepSeek Harness）**没有工具级白名单**：agent 挂了 MCP，`tools/list` 里的
10 个工具就全部可见可调；本服务也**按设计**不拦写（ADR 0003）。

于是这条铁律目前只能靠 prompt 与事后审计兜着，写边界是纪律不是结构。

同时非实施角色**确实需要读**：关键词策略师必须用 `KRService.getKRByQuery` /
`getKRCustom` / `PvSearchFunction.getPvSearch` 拓词，否则词包只能凭空生成。
"某些 agent 不挂 MCP"不是选项。

需要的是**两个入口**：一个只读、一个全量。

ADR 0003 在"无写拦截"风险的应对里已经预留了这条路（"生产可配只读模式"），
本次把它落成具体的部署形态，见 [ADR 0011](../../../../docs/decisions/0011-readonly-endpoint.md)。

## 变更内容

1. **第二个 `MCPServer` 实例**（`build_server(readonly=True)`），只注册 7 个只读工具，
   挂在 `/mcp/readonly`；协议、鉴权、tenancy 与 `/mcp` 完全一致。
2. **`execute_capability` 单点守卫**：目标能力 `mutating` 不是 `"false"` 一律拒绝
   （`unknown` 按写处理），拒绝走统一失败信封，文本含能力 key、`mutating=<值>`、
   "改用全量端点"。
3. **按只读裁剪工具注册**：`bind_account` / `share_account` / `revoke_share` 不注册
   （它们改变授权与账户可见性）。
4. **HTTP 装配**：父 Starlette 应用里同时挂两条路由，由进程自己起 uvicorn
   （`MCPServer.run()` 不接受外部父 app）。已在本机用 `httpx.ASGITransport` 验通：
   两入口都能 `initialize` 并各自 `tools/list`；两个坑（子应用 lifespan 不会自动启动、
   `Mount` 会先吃一个 307）与解法见 [design D6 / 代码形状](design.md)。
5. **配置**：`MYBMO_MCP_READONLY=1` 时启用，路径 `MYBMO_MCP_READONLY_PATH`（默认
   `/mcp/readonly`）。**不设开关时行为与现在逐字节一致**。

### 工具面

| 工具 | `/mcp` | `/mcp/readonly` |
|---|---|---|
| `search_capabilities` | ✅ | ✅ |
| `get_capability_schema` | ✅ | ✅ |
| `execute_capability` | ✅ | ✅ 仅当 `mutating == "false"` |
| `list_accounts` | ✅ | ✅ |
| `auth_status` | ✅ | ✅ |
| `list_shares` | ✅ | ✅ |
| `query_audit` | ✅ | ✅ |
| `bind_account` | ✅ | ❌ |
| `share_account` | ✅ | ❌ |
| `revoke_share` | ✅ | ❌ |
| **合计** | **10** | **7** |

## 不做

- **不在工具层做通用写拦截**，`/mcp` 行为不动（ADR 0003 / 0011）。只读入口是装配差异，
  不是新的策略层；将来要按"用户/租户/角色"裁剪工具，是另一个变更。
- **不新增审计枚举**：拒绝记 `state=denied`（已有），不做迁移。
- 不按租户/用户收窄 `query_audit`（沿用现语义，已按用户隔离）。
- 不做 UI、不做 agent 侧编排（那属于 `mybmo-agents` 仓库）。
- 不拆分进程/端口（同端口两条路由，见 design D6）。

## 影响面

| 区域 | 变化 |
|---|---|
| `config.py` / `.env.example` / `deploy/.env` | 两个新开关（含 15 分钟路径校验） |
| `server.py` | `build_server(readonly=)`、只读版 instructions、`_http_app()` 装配、`_root` 分支 |
| `tools/__init__.py` | `register_all(..., readonly=)` |
| `tools/accounts.py` | `register(mcp, *, readonly=)`：跳过三个写工具 |
| `tools/capability.py` | `execute_capability` 只读守卫 + `denied` 审计；`register(mcp, *, readonly=)` |
| `docs/` | 新增 ADR 0011；`auth-contract.md` / `architecture.md` / README / `openspec/project.md` 同步 |
| 存储 / 迁移 / 能力表 | **零改动** |

## 依赖与待确认（不阻塞开工）

1. **客户端令牌缓存按 URL 分目录**：`mcp-remote` 的 hash 含 server URL，所以 `/mcp` 与
   `/mcp/readonly` **各要授权一次（全系统共 2 次）**；同一 OS 用户下所有成员共用同一份
   `tokens.json`，不是每成员一次。已在 design「已知副作用」里写明为什么压不到 1 次。
2. **路径由反代透传**：Caddy 当前只转 `/mcp`（见 `docs/auth-contract.md`），
   需要确认 `/mcp/readonly` 也在转发范围内——属 `pandabase-infra` 仓库的配置项。

## 验收

见 [`tasks.md`](tasks.md) 第 5 节；对应需求单 §六 的 8 条用例。
