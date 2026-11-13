# 回信：只读 MCP 入口已上线（`/mcp/readonly`）

> 致：`mybmo-agents`
> 答复：[`docs/req-mybmo-mcp-readonly-endpoint.md`](../../../../docs/req-mybmo-mcp-readonly-endpoint.md)
> 状态：✅ 已实现并上线（2026-11-13，pandabase-dev01）
> 决策：[ADR 0011](../../../../docs/decisions/0011-readonly-endpoint.md)

## 一、结论

R1–R4 全部落地，端点现在可以用：

```
https://baiduads.pandabase.top/mcp/readonly   只读（7 工具）
https://baiduads.pandabase.top/mcp            全量（10 工具，行为未变）
```

同一套鉴权、同一套 tenancy、同一份 OAuth 元数据；**不需要任何新身份、新授权流程**。

工具面（`tools/list` 实测）：

| 工具 | `/mcp` | `/mcp/readonly` |
|---|---|---|
| `search_capabilities` / `get_capability_schema` | ✅ | ✅ |
| `execute_capability` | ✅ | ✅ 仅 `mutating == "false"` |
| `list_accounts` / `auth_status` / `list_shares` / `query_audit` | ✅ | ✅ |
| `bind_account` / `share_account` / `revoke_share` | ✅ | ❌ **不注册** |
| 合计 | **10** | **7** |

## 二、你们 §八 的四个开放问题

**1. 独立路由还是独立端口/进程？** → **独立路由**（同端口 `/mcp/readonly`）。
反代不用改（这台机的 Caddy 用兜底 `handle`，天然覆盖）。

**2. 被拒调用是否进 `audit_log`？`state` 用现成枚举还是新增 `forbidden`？** →
**进，用现成的 `denied`，不新增枚举、不做迁移**。实测三条：

```
2026-11-13 14:28:13 | test01 | ImageManageService.uploadImage | denied | readonly endpoint：…mutating=true…
```

**3. `query_audit` 是否按租户收窄？** → **不收窄，保持原语义**（本来就按用户隔离：
普通用户只见自己的调用，owner 可见自己账户下的全部）。

**4. 需要 `bind_account` 的替代品吗？** → **不需要，接受你们说的限制**：只读入口没有
`bind_account`，重新授权请人工走全量入口或后台。**建议把这一点写进你们的值班流程**——
只读入口的 `auth_status` 会给出 `needs_reauth: true`，但没人能替它重新绑定。

## 三、行为契约（请按这个写你们的处理逻辑）

被拒时返回的是**结构化失败信封**（本项目所有工具的既有约定，不抛 MCP 异常）：

```json
{
  "ok": false,
  "error": {
    "code": null,
    "message": "readonly endpoint：能力 'ImageManageService.uploadImage' 的 mutating=true，本入口只允许只读能力，已拒绝。写操作请改用全量端点 /mcp。",
    "hint": "只读入口的 execute_capability 仅接受 mutating=false 的能力",
    "capability": "ImageManageService.uploadImage",
    "mutating": "true",
    "endpoint": "readonly"
  }
}
```

差异点提醒：需求单 §五 希望是 `isError: true` 的 MCP 工具错误，我们按本项目约定返回
`ok:false` 信封（`isError` 是 `false`）。**判定请用 `ok`，不要用 `isError`**，否则会漏判。
文本四要素齐全（`readonly endpoint` + 能力 key + `mutating=<值>` + 建议改用全量端点），
可以与"没权限"（tenancy 的 `AccessDenied`）一眼区分。

两个细节：

- **拒绝发生在参数校验之前**，一个字节都不会发到百度（`transport.calls == []` 有用例保证）。
- 想让 agent 自己知道"这个能力是写"，用 `get_capability_schema` 看 `mutating` 字段即可，
  只读入口也能查写能力的 schema。

## 四、两条你们要知道的边界

**1. `mutating=unknown` 会被拒，而全表只有一条**：`PreAuditService.materialPreAudit`
（`sms` 线，不在我们承诺的电商覆盖内）。如果你们在电商线上确实需要它，先找我们实测确认
它是只读，我们把能力表里那条标成 `false`——**不要在入口开洞**。

**2. 客户端授权是"每个 URL 一次"，共 2 次，之后全员复用。**
`mcp-remote` 的令牌缓存键是 `md5(serverUrl|headers|params|…)`：

- `/mcp` 与 `/mcp/readonly` 两个 hash ⇒ 各授权一次（**不是每个成员一次**）；
- 同一 OS 用户下所有成员共用 `~/.mcp-auth/mcp-remote-v1/*_tokens.json`；
- 服务端同一个令牌对两个入口都有效（已实测），客户端只是按 URL 缓存。

我们评估过"压到 1 次"，结论是**做不到**：hash 的输入就是 URL + headers 等，任何能让服务端
区分只读角色的信号都会多出一个 hash；反之"同 URL + 同 token"意味着服务端在连接层无法区分
调用方，工具面就只能一样，结构保证会退化回按身份裁剪。真要 1 次，只能去掉 `mcp-remote`、
改 DSH 直连 `streamable-http` + 自带令牌刷新（另立变更，约 50 行脚本 + 一个刷新职责）。

## 五、你们 §七 的 preset

可以直接用，`serverName: mybmo_read` 那条指向 `/mcp/readonly` 即可。补三点：

1. 首次接入会弹浏览器，**点一次**（Casdoor 会话在的话是"点一下"）；
2. `--static-oauth-client-info` 里的 client_id 保持 `709a8e16656a223a1ab7` 不变；
3. 成员 8 的全量 preset 用 `/mcp`，`serverName: mybmo_exec`。

下线 Host 级共享行 `mcp-pandabase-mybmo` 这件事我们支持——那正是"写边界由结构保证"的最后一环。

## 六、验收证据（可复现）

2026-11-13 用 test01 的真实令牌、走公网域名（即经 Caddy）：

| # | 用例 | 结果 |
|---|---|---|
| 1 | 只读入口 `tools/list` | 7 个，不含三个写工具 |
| 2 | `execute_capability("ImageManageService.uploadImage")` | 拒绝，`mutating=true` |
| 3 | `execute_capability("CampaignShoppingService.addCampaignShopping")` | 拒绝（单测覆盖） |
| 4 | `execute_capability("KRService.getKRByQuery", {query:"咖啡机"})` | **成功**：pcPV 2291 / mobilePV 8407 / recommendPricePc 1.0 |
| 5 | `mutating=unknown` 的能力 | 拒绝（单测覆盖） |
| 6 | 其余只读工具 | 全部成功（`list_accounts` 返回 `main`, my_permission=admin） |
| 7 | 全量入口回归 | 10 个工具，写能力可调 |
| 8 | 不设开关 | `/mcp/readonly` → 404 |

## 七、顺手修掉的两个线上缺陷（与你们有关）

上线过程中发现并已修复，都会影响你们的观感：

1. **只读入口任何令牌都 401**（我们自己的装配 bug：搬了 `routes` 没搬鉴权 `middleware`）——
   已修。如果你们前几天试过只读入口并吃到 401，那不是你们的配置问题。
2. **既有缺陷：账户令牌过期一次之后永久失效**（刷新流程提前关闭 HTTP 客户端，新令牌没落库）。
   表现为"某个账户突然所有调用都失败"，只能重绑恢复。已修，现在会自动刷新并自愈。

## 八、下一步（你们侧）

1. 成员 1–7 的 preset 指向 `/mcp/readonly`，成员 8 指向 `/mcp`；
2. 每个 URL 各点一次授权（共 2 次，同机全员复用）；
3. 下线 Host 级共享行 `mcp-pandabase-mybmo`；
4. 值班流程里加一条：只读角色看到 `needs_reauth: true` → 找人工重绑。
