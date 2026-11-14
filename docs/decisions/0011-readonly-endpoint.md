# ADR 0011：只读 MCP 入口是「装配形态」，不是策略层

- 状态：已接受
- 日期：2026-11-13
- 决策者：项目负责人
- 关联：[ADR 0003](0003-capability-only.md)（只做能力层）、
  [ADR 0004](0004-multi-tenant-accounts.md)（多租户账户）、
  [`archive/2026-11-13-readonly-endpoint`](../../openspec/changes/archive/2026-11-13-readonly-endpoint/proposal.md)、
  对方需求单 `openspec/changes/archive/2026-11-13-readonly-endpoint/req-from-mybmo-agents.md`

## 背景

ADR 0003 定下"能力层不做业务编排、不做写审批"，`mutating` 只是给调用方看的元数据，
并在"无写拦截"的风险应对里留了一句"生产可配只读模式"。

现在 `mybmo-agents` 的 8 角色链路把它顶到了台面上：**单一写入口**要求只有实施操作员能调
`mutating: true` 的能力，但执行环境（DeepSeek Harness）没有工具级白名单——挂了 MCP，
10 个工具就全部可调。同时非实施角色确实需要读（拓词、取 schema、看账户）。

于是必须在服务端提供"只读"这件事。问题在于：**它归哪一层？**

## 决策

**只读入口是一个独立的服务实例（装配差异），不是一个通用拦截策略。**

具体三条：

1. **两条入口，同端口**：`/mcp`（10 个工具，全量）与 `/mcp/readonly`（7 个工具）。
   只读入口由 `build_server(readonly=True)` 起**第二个 `MCPServer` 实例**，
   工具集在**注册时**就少了三个写工具，`execute_capability` 带一个 `mutating` 白名单守卫。
2. **不引入策略层**：没有规则文件、没有角色表、没有"给某用户裁某个工具"。
   守卫判的是**能力自身的读写属性**（`mutating`），与"谁在调"无关——那是 tenancy 的活，
   两者不交叉。
3. **`/mcp` 行为不变**。不设 `MYBMO_MCP_READONLY` 时，进程走的还是原来的
   `server.run(transport="streamable-http")`，连装配路径都不换。

## 备选方案对比

| 方案 | 否决/采纳理由 |
|---|---|
| **两个实例 + 两条路由** | **采纳**。工具面在注册期就不同，"看不到"多于"调不动"，客户端不会误用 |
| `tools/list` 按请求身份过滤 + 同一实例 | 要在协议层做策略，等于把规则塞进工具层；且过滤逻辑与工具定义耦合，难测 |
| 启动参数 `--readonly`（一个进程只读） | 一份部署只能二选一，而需求要的是**同时**存在两个入口；除非跑两套容器 |
| 按用户/租户/角色裁剪工具 | 真正的策略层，需要"规则存哪、谁维护"的整套东西；需求方也明确说那是另一个话题 |
| 完全不在服务端做（只靠 prompt 纪律） | 就是现状：写边界是纪律不是结构，一次幻觉就穿透 |

## 后果

### 正面

- "成员没有写工具"从纪律变成结构：客户端的 `tools/list` 里根本没有那三个工具
- 与 ADR 0003 不冲突：能力层仍然不编排、不审批，只多了一个**部署形态**
- 零存储改动：复用 `audit_log.state = denied`、复用同一套 OAuth/tenancy
- 回滚成本为零：删环境变量即回到单入口

### 负面 / 需要承担

| 风险 | 说明 | 应对 |
|---|---|---|
| 双实例 = 双份 instructions/工具集，可能漂移 | 两份注册逻辑各写一遍就会不一致 | 同一份 `register_all(..., readonly=)`；测试双向断言 10 / 7 |
| 装配变复杂 | `MCPServer.run()` 不收父 app，需要自己组父 Starlette + uvicorn | 只在开关打开时走新路径（ADR 决策 3），关闭时路径原样 |
| 有人顺着它扩大成策略层 | "既然能只读，那也能按角色裁"——一步就滑回工具层拦截 | 本 ADR 明确边界：**只按能力读写属性切**。要按身份切，另立 ADR |
| 只读入口挡住 `mutating=unknown` 的能力 | 目前全表只有 `PreAuditService.materialPreAudit`（`sms` 线，不在承诺覆盖内） | 若电商线真需要，先实测确认无害再把该能力标 `mutating=false`，**不在入口开洞** |
| 客户端要各自授权一次 | `mcp-remote` 按 server URL 缓存令牌 | 写进上线清单；属客户端行为，不是服务端缺陷 |

## 未决问题

- [x] 反代：**无需改动**。这台机是共享宿主机形态，`baiduads.pandabase.top` 站点用的是兜底
      `handle { reverse_proxy }`，`/mcp/readonly` 天然在转发范围内（2026-11-13 实测公网 401 打通）
- [ ] 将来若按"用户/角色"裁剪工具，是否仍以第二实例承载（还是引入真正的策略层）
- [ ] 想做到"客户端只授权一次"就得去掉 `mcp-remote`、让 DSH 直连 streamable-http 并自带令牌刷新；
      本次决定不付这个成本（见变更包 design「已知副作用」）

## 修订（2026-11-14）

`pandabase.top` 的 ICP 备案审核期间，公网 443 被备案拦截（RST）、80 被 302 到拦截页，
infra 把对外入口临时挪到 **`:8443`**（pandabase-infra ADR 0005）。因此本文提到的公网路径在过渡期为：

- `https://baiduads.pandabase.top:8443/mcp`
- `https://baiduads.pandabase.top:8443/mcp/readonly`

**决策本身不变**：「反代无需改动、`/mcp/readonly` 天然在转发范围内」在加端口后同样成立
（仍由同一站点兜底 `handle { reverse_proxy }` 承接）。备案通过后切回 443 无端口形态，由 infra 通知。
正文按决策记录保留原样，本小节只做口径补充。
