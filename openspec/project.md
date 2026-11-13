# mybmo-mcp

## 是什么

百度营销开放平台的 MCP 能力网关。412 个接口收进 10 个工具，用检索代替枚举；百度令牌只活在
服务端，客户端拿的是本服务自己的身份。

部署、身份与数据库在 [`pandabase-infra`](https://github.com/bianzhishui/pandabase-infra)，
本仓库只承载业务代码与业务决策。

## 用户与模式

| 模式 | 场景 | 身份来源 |
|---|---|---|
| `local` | 本机 stdio，单用户 | 进程即身份，不校验令牌 |
| `oidc` | 公网 HTTP，多客户端 | 校验 Bearer JWT，身份来自 `sub` |

线上以 `oidc` 运行。授权表**双后端**：默认文件式 `data/tenancy.json`；
`MYBMO_TENANCY_BACKEND=pg` 时读 PostgreSQL（线上已切 PG），见
[`multi-tenant-persistence`](changes/archive/2026-11-11-multi-tenant-persistence/proposal.md)。

## 约束

| 约束 | 影响 |
|---|---|
| 百度令牌按账户发放，刷新可能轮换 | 凭据按账户隔离，刷新加锁，失败不重试旧值 |
| `userName` 必须是令牌所属账户的中文名 | 账户名不能猜，绑定后必须落库（错误码 81020） |
| MCC 子账户复用父账户令牌，但 `userName` 填子账户 | 取令牌要沿 `parent_account_id` 上溯 |
| MCP 规范要求校验 audience | 不能把百度令牌当接入令牌；`aud` 是 client_id，按白名单处理 |
| 反代不做鉴权 | `/mcp` 的令牌校验必须在进程内完成 |
| 单机、单人维护 | 不引需要专职运维的组件；不追求 HA |
| IdP 组织之间有边界（built-in / pandabase） | 在鉴权层校验（aud 白名单 + `MYBMO_OIDC_ORGS`）；授权表只表达"用户 × 账户 × 档位" |
| 百度账户归绑定者私有 | `tenancy.json` 的 `accounts[].owner`；owner 隐式 admin，分享只能 read/write |

## 边界

**做**：能力检索、参数 schema、忠实执行、账户管理、令牌生命周期、审计。

**覆盖范围**：只验证并承诺**电商推广线**（购物推广：`*ShoppingService` + 电商/一站式报表）。
搜索推广、信息流推广的接口仍在能力表里（检索、取 schema、执行都能调），但**未经实测**，
不对外承诺；它们的验证券已从 `live-validation` 移出。

**不做**：业务编排与写审批（[ADR 0003](../docs/decisions/0003-capability-only.md)）、
基础设施运维、客户端 UI。

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/architecture.md`](../docs/architecture.md) | 三层信任边界、时序、数据模型、凭据生命周期 |
| [`docs/auth-contract.md`](../docs/auth-contract.md) | 与 infra 的鉴权契约（不变量、环境变量、端点、卷布局） |
| [`docs/baidu-behavior.md`](../docs/baidu-behavior.md) | 百度接口行为实测结论（身份/分页/字段/错误码） |
| [`docs/reports.md`](../docs/reports.md) | 推广报告速查：参数、reportType 总表、列字典 |
| [`docs/audit.md`](../docs/audit.md) | 调用审计：字段、体积、查询与隔离 |
| [`docs/image-management.md`](../docs/image-management.md) | 图片资产管理：接口、去重、最终一致 |
| [`docs/campaign-shopping.md`](../docs/campaign-shopping.md) | 电商计划读写：标准字段表与写前校验 |
| [`docs/keyword-packages.md`](../docs/keyword-packages.md) | 电商关键词包：8 个接口（含导出、绑定计划、删词）全部实测 |
| [`docs/keyword-planner.md`](../docs/keyword-planner.md) | 关键词规划师：接口实测、参数取值范围、异步串行限制 |
| [`ADR 0002`](../docs/decisions/0002-tech-stack.md) | 技术栈与理由 |
| [`ADR 0003`](../docs/decisions/0003-capability-only.md) | 只做能力层 |
| [`ADR 0004`](../docs/decisions/0004-multi-tenant-accounts.md) | 多租户账户模型与 MCC 子账户 |
| [`ADR 0005`](../docs/decisions/0005-sdk-poc.md) | SDK 只做元数据源 |

## 当前状态

- 412 个能力、92 条精写说明、598 个模型；**33 条已实测**（`verified` 标记）；测试 295 passed
- `oidc` 模式跑通授权码 + PKCE；9 个 MCP 工具（含 `bind_account` / `share_account`）
- 已绑定 1 个真实百度账户；电商推广线**写操作**（计划暂停/复制/加图、词包增删改绑）与
  报表异步任务均已实测，见 [`live-validation`](changes/archive/2026-11-11-live-validation/proposal.md)（32/32 完成）
- 授权表已切 PostgreSQL：users 2 / accounts 1 / grants 1，判权实测（owner=admin、write 拒绝）

## 变更状态

已完成的变更归档在 `changes/archive/`，`changes/` 只留在进行中的。

| 变更 | 状态 | 备注 |
|---|---|---|
| [`live-validation`](changes/archive/2026-11-11-live-validation/proposal.md) | ✅ 已完成 | 32/32 任务；结论进 `docs/baidu-behavior.md` 等 |
| [`multi-tenant-persistence`](changes/archive/2026-11-11-multi-tenant-persistence/proposal.md) | ✅ 已完成（MCC 暂缓） | PG 后端已上线，文件后端可回滚 |
| [`audit-log-persistence`](changes/archive/2026-11-12-audit-log-persistence/proposal.md) | ✅ 已完成 | `audit_log` 表 + `query_audit` 工具已上线 |
