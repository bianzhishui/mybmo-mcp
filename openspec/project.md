# mybmo-mcp

## 是什么

百度营销开放平台的 MCP 能力网关。389 个接口收进 5 个工具，用检索代替枚举；百度令牌只活在
服务端，客户端拿的是本服务自己的身份。

部署、身份与数据库在 [`pandabase-infra`](https://github.com/bianzhishui/pandabase-infra)，
本仓库只承载业务代码与业务决策。

## 用户与模式

| 模式 | 场景 | 身份来源 |
|---|---|---|
| `local` | 本机 stdio，单用户 | 进程即身份，不校验令牌 |
| `oidc` | 公网 HTTP，多客户端 | 校验 Bearer JWT，身份来自 `sub` |

线上以 `oidc` 运行。多租户的**授权**目前是文件式 `data/tenancy.json`，落库见
[`multi-tenant-persistence`](changes/multi-tenant-persistence/proposal.md)。

## 约束

| 约束 | 影响 |
|---|---|
| 百度令牌按账户发放，刷新可能轮换 | 凭据按账户隔离，刷新加锁，失败不重试旧值 |
| `userName` 必须是令牌所属账户的中文名 | 账户名不能猜，绑定后必须落库（错误码 81020） |
| MCC 子账户复用父账户令牌，但 `userName` 填子账户 | 取令牌要沿 `parent_account_id` 上溯 |
| MCP 规范要求校验 audience | 不能把百度令牌当接入令牌；`aud` 是 client_id，按白名单处理 |
| 反代不做鉴权 | `/mcp` 的令牌校验必须在进程内完成 |
| 单机、单人维护 | 不引需要专职运维的组件；不追求 HA |

## 边界

**做**：能力检索、参数 schema、忠实执行、账户管理、令牌生命周期、审计。

**不做**：业务编排与写审批（[ADR 0003](../docs/decisions/0003-capability-only.md)）、
基础设施运维、客户端 UI。

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/architecture.md`](../docs/architecture.md) | 三层信任边界、时序、数据模型、凭据生命周期 |
| [`docs/auth-contract.md`](../docs/auth-contract.md) | 与 infra 的鉴权契约（不变量、环境变量、端点、卷布局） |
| [`ADR 0002`](../docs/decisions/0002-tech-stack.md) | 技术栈与理由 |
| [`ADR 0003`](../docs/decisions/0003-capability-only.md) | 只做能力层 |
| [`ADR 0004`](../docs/decisions/0004-multi-tenant-accounts.md) | 多租户账户模型与 MCC 子账户 |
| [`ADR 0005`](../docs/decisions/0005-sdk-poc.md) | SDK 只做元数据源 |

## 当前状态

- 389 个能力、50 条精写说明、585 个模型；205 个测试离线全绿
- `oidc` 模式已跑通授权码 + PKCE（真实令牌换发与 `/mcp` 鉴权已验）
- **没有任何一次真实百度接口调用**——这是 v1 验收的唯一阻塞项，见
  [`live-validation`](changes/live-validation/proposal.md)

## 进行中的变更

| 变更 | 状态 | 触发条件 |
|---|---|---|
| [`live-validation`](changes/live-validation/proposal.md) | 待开始 | 有可用的百度账号 |
| [`multi-tenant-persistence`](changes/multi-tenant-persistence/proposal.md) | 未开工 | 出现第二个真实用户 |
