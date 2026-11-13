# 需求：mybmo-mcp 提供只读 MCP 入口（`/mcp/readonly`）

> **状态**：✅ 已实现并上线（2026-11-13，pandabase-dev01），归档在
> [`openspec/changes/archive/2026-11-13-readonly-endpoint/`](../openspec/changes/archive/2026-11-13-readonly-endpoint/proposal.md)；
> 决策见 [ADR 0011](decisions/0011-readonly-endpoint.md)（只读入口是装配形态，不是策略层）。
> 我方回信见 [`reply-to-mybmo-agents.md`](../openspec/changes/archive/2026-11-13-readonly-endpoint/reply-to-mybmo-agents.md)。

> 提出方：`mybmo-agents`（电商推广多 Agent 工作流）
> 关联设计：`mybmo-agents:docs/dsh-multi-agent-v1.md` §5.2 / §5.3
> 优先级：P2（不阻塞首单灰度，但决定"单一写入口"能否从纪律变成结构）

---

## 一、背景

`mybmo-agents` 是一条 **8 角色协作**链路（商品分析 → 词包 → 文案 → 素材 → 计划 → 参数 → 审计 → 实施）。
其中一条核心铁律是**单一写入口**：所有 `mutating: true` 的能力只允许「实施操作员」一个角色调用。

问题在于执行环境（DeepSeek Harness）**没有工具级白名单**：

- `dsh-mcp-client` 的配置项里没有 allow/deny 工具列表；
- `dsh-agent-preset` 也没有 `toolFilter`；
- 所以只要 agent 挂了 MCP，`tools/list` 里的 10 个工具**全部可见可调**。

而 mybmo-mcp 目前也不拦写：`tools/__init__.py` 明确「工具层不做写操作拦截，写与不写由调用方决定」，
`auth/tenancy.required_permission()` 只判"读/写档位"，而工作流用的身份是 `admin`，读写都满足。

**结果**：非实施角色理论上能直接调 `uploadImage` / `addCampaignShopping`，写边界只能靠 prompt 和事后审计兜着。

同时，非实施角色**确实需要读**——例如「关键词策略师」必须用 `KRService.getKRByQuery` /
`getKRCustom` / `PvSearchFunction.getPvSearch` 拓词，否则词包只能凭空生成。

所以需要的不是"某些 agent 不挂 MCP"，而是**两个入口：一个只读、一个全量**。

## 二、需求

| # | 需求 | 说明 |
|---|---|---|
| **R1** | 暴露一个**只读 MCP 入口** | 与现有 `/mcp` 同协议（streamable-http）、同鉴权，但只注册只读工具 |
| **R2** | 只读入口上的 `execute_capability` **硬拒写能力** | 判定用能力表已有的 `mutating`；`mutating in {"true", "unknown"}` 一律拒绝（`unknown` 按写处理，取严） |
| **R3** | 复用现有鉴权，不引入新身份 | 同一个 OAuth token_verifier / 同一套 tenancy；只读入口不要求额外授权流程 |
| **R4** | 默认入口行为**不变** | `/mcp` 保持全量；只读入口是**可选开关**，关闭时完全不挂载 |

### 只读入口应有的工具清单

| 工具 | 只读入口 |
|---|---|
| `search_capabilities` | ✅ |
| `get_capability_schema` | ✅ |
| `execute_capability` | ✅ **但仅当目标能力 `mutating == "false"`** |
| `list_accounts` | ✅ |
| `auth_status` | ✅ |
| `list_shares` | ✅ |
| `query_audit` | ✅（只读数据库查询） |
| `bind_account` | ❌ 不注册 |
| `share_account` | ❌ 不注册 |
| `revoke_share` | ❌ 不注册 |

理由：`bind_account` / `share_account` / `revoke_share` 改变授权与账户可见性，属写；其余是纯读。

## 三、非目标（明确不做）

1. **不改 `/mcp` 的默认行为**，不在工具层做通用拦截——尊重本项目"能力层不做编排/拦截"的既有定位（ADR 0003）。只读入口是一个**独立部署形态**，不是新的通用策略层。
2. **不做按用户/按租户的工具裁剪**（那是另一个话题；这里只按能力读写属性切）。
3. 不做 UI、不做 agent 侧编排。

## 四、建议实现（最小改动）

```python
# server.py
def build_server(*, with_auth: bool = True, readonly: bool = False) -> MCPServer:
    ...
    register_all(server, readonly=readonly)

# tools/__init__.py
def register_all(mcp: MCPServer, *, readonly: bool = False) -> None:
    accounts.register(mcp, readonly=readonly)     # skip bind_account/share_account/revoke_share
    capability.register(mcp, readonly=readonly)   # execute_capability 加 guard
    audit.register(mcp)
```

`execute_capability` 的守卫（放在参数校验之后、真正调用百度之前）：

```python
from mybmo_mcp.auth.tenancy import required_permission, PERMISSION_READ
if readonly and required_permission(cap.mutating) != PERMISSION_READ:
    raise ToolError(f"readonly endpoint: capability {cap.key!r} is mutating={cap.mutating}, "
                    f"refusing. Use the full endpoint for write capabilities.")
```

**挂载方式**（两者取其一，取决于 MCP SDK 的多实例支持）：

- A：起第二个 `MCPServer` 实例，仅注册只读工具，挂到 `/mcp/readonly`（推荐，隔离最彻底）；
- B：同一个实例按请求路径分流（改造成本高，不推荐）。

**配置**：

| 变量 | 默认 | 含义 |
|---|---|---|
| `MYBMO_MCP_READONLY` | 空（关闭） | 置 `1` 时挂载只读入口 |
| `MYBMO_MCP_READONLY_PATH` | `/mcp/readonly` | 只读入口路径 |

关闭时行为与现在**逐字节一致**（回归测试要覆盖）。

## 五、行为契约（拒绝时返回什么）

- 返回 MCP 工具错误（`isError: true`），文本里必须包含：`readonly endpoint`、能力 key、`mutating=<值>`、以及"请改用全量端点"。不要只写"权限不足"——调用方要能一眼分清是"没权限"还是"能力不存在"。
- **建议**仍在 `audit_log` 记一条，`state` 用现有枚举里最接近的失败态（若需要新增 `forbidden` 枚举值，请在该项目内自行决定，并提出迁移）。

## 六、验收标准（可逐条测）

| # | 用例 | 期望 |
|---|---|---|
| 1 | 只读入口 `tools/list` | 恰好 7 个工具，不含 `bind_account` / `share_account` / `revoke_share` |
| 2 | 只读入口 `execute_capability("ImageManageService.uploadImage", …)` | 拒绝，错误文本含 `readonly endpoint` 与 `mutating=true` |
| 3 | 只读入口 `execute_capability("CampaignShoppingService.addCampaignShopping", …)` | 拒绝，同上 |
| 4 | 只读入口 `execute_capability("KRService.getKRByQuery", {…})` | 成功返回拓词结果 |
| 5 | 只读入口 `execute_capability(<mutating=unknown 的能力>)` | 拒绝（按写处理） |
| 6 | 只读入口 `search_capabilities` / `get_capability_schema` / `list_accounts` / `auth_status` | 全部成功 |
| 7 | 全量入口 `/mcp` 回归 | 10 个工具、写能力可调，与改造前一致 |
| 8 | `MYBMO_MCP_READONLY` 未设置 | `/mcp/readonly` 404，其余行为不变 |

## 七、我方（DSH）侧配套（拿到端点后只改配置）

```yaml
# 成员1-7 的 preset
- id: mcp-mybmo-read
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: mybmo_read
    transport: stdio
    command: npx
    args: ['-y','mcp-remote@latest','https://<host>/mcp/readonly',
           '--transport','http-only','--disable-resource-parameter',
           '--static-oauth-client-info','{"client_id":"709a8e16656a223a1ab7"}']
    toolCallTimeoutMs: 120000

# 成员8 的 preset 用全量 /mcp（serverName: mybmo_exec）
```

同时下线 Host 级共享行 `mcp-pandabase-mybmo`，届时"成员没有写工具"由结构保证。

## 八、开放问题（请 mybmo-mcp 决策后回我）

1. 只读入口用**独立路由**还是**独立端口/进程**？（我们两种都能接，独立路由更省事）
2. 被拒调用是否要进 `audit_log`？若要，`state` 用现成枚举还是新增 `forbidden`？
3. `query_audit` 在只读入口是否也要按租户隔离范围收窄？（现在是只读查询，我们倾向保留原语义）
4. 是否需要"只读入口也不允许 `bind_account` 的替代品"——即我们这边的用户重新授权时，只能由人工在后台做？我们接受这个限制。

## 九、工作量估计

0.5–1 人日（含上面 8 条验收用例 + 全量入口回归）。改动集中在 `server.build_server()` / `tools/__init__.py` / `tools/accounts.py` / `tools/capability.py` 与配置项，不触碰存储、审计表结构与既有能力表。
