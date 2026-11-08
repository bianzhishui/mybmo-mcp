# ADR 0004：多租户账户模型与能力执行语义

- 状态：已接受
- 日期：2026-11-07
- 决策者：项目负责人
- 关联：infra 仓库 `docs/decisions/0003-identity-provider.md`（Casdoor）、
  [ADR 0003](0003-capability-only.md)（能力层）、[`docs/auth-contract.md`](../auth-contract.md)（鉴权契约）

## 背景

ADR 0003 确定了「只提供能力」的定位，但账户属于谁、谁能用、长尾能力怎么暴露仍未定。
本项目**面向外部客户**（不是内部工具），租户隔离与授权必须在写第一行代码前固定，
否则后期改表结构要迁移凭据数据。

## 决策

### D1 账户归属于租户，按 grant 授权给用户

账户不是某个人的私有物，而是**租户的资产**；用户通过 `account_grants` 获得使用权。

```sql
tenants         (id, name, plan, status, created_at)
users           (id, tenant_id, idp_subject, email, display_name, role, status, created_at)
baidu_accounts  (id, tenant_id, alias, kind, baidu_user_id, user_name,
                 parent_account_id, app_id, secret_key_cipher, refresh_token_cipher,
                 access_token_expires_at, status, created_at, updated_at)
account_grants  (user_id, account_id, permission, created_at)   -- read | write | admin
audit_log       (id, tenant_id, account_id, user_id, capability, request_id,
                 state, payload_digest, error, created_at)
```

关键约束：

- `UNIQUE (tenant_id, alias)` —— 别名在**租户内**唯一，不同租户可以有同名账户（`axd` 在 A 公司和 B 公司是两个账户）
- `UNIQUE (tenant_id, idp_subject)` —— 一个 Casdoor 身份在一个租户内只对应一个用户
- 所有查询带 `tenant_id`，不靠应用层 `if` 判断

**权限档位与能力的映射**：

| grant | 可执行的能力 |
|---|---|
| `read` | 只读能力（`mutating = false`） |
| `write` | 只读 + 写能力 |
| `admin` | 另可管理账户与授权（绑定、解绑、改授权） |

`mutating = unknown` 的能力**按写处理**——判不准时收紧，不放开。

### D2 绑定形态支持两种：独立账户与 MCC 子账户

百度有管家账户（MCC），一个令牌可以管理一批子账户。两种都要支持：

| `kind` | 含义 | 令牌 | 调用时的 `userName` |
|---|---|---|---|
| `standalone` | 独立推广账户，自己授权 | 自己的 `app_id` + `refresh_token` | 该账户中文名 |
| `mcc_child` | 管家账户下的子账户 | **复用** `parent_account_id` 的令牌 | **子账户**中文名 |

实现要点：

- `mcc_child` 的 `refresh_token_cipher` 为空，取凭据时沿 `parent_account_id` 上溯一层
- 调用时 `userName` 必须是子账户中文名，填成主账户会报 `81020`（"userName 与令牌不属同一账户"）
- 刷新令牌只对主账户做一次，子账户共享结果——这天然避免了同一令牌被并发轮换
- 解绑主账户前必须先处理子账户，否则子账户会变成孤儿

### D3 用户来源：Casdoor + 本地 `users` 映射，不依赖 org claim

Casdoor Organization 与 `tenant` 一对一，但**租户判定以本地 `users` 表为准**：

```text
JWT.sub → users.idp_subject → users.tenant_id → 该请求可见的账户集合
```

不直接读 Casdoor 令牌里的组织字段，原因是该字段的格式跨版本变过，而 `sub` 是稳定契约。
Casdoor 侧的 org 主要用于管理台分组与登录页展示。

### D4 能力描述：399 条自动生成骨架，高频精写

| 层 | 内容 | 来源 |
|---|---|---|
| 骨架 | 能力 key、方法名、参数类型、产品线、`mutating` | 从 SDK 内省自动生成 |
| 精写 | 参数的**语义**：单位是元还是分、`campaignId` 与 `adgroupId` 的层级、副作用、配额 | 人工，按使用频次排序 |

自动生成保证"每个能力都有描述，不是空白"；精写决定"agent 能不能一次调对"。
两者缺一不可：只有骨架，长尾全靠猜；只靠人工，399 条写不完。

精写优先级的判据是**调用频次**，不是接口重要性——先用 curated 清单里那些高频域
（账户、投放、关键词、报表）打底，其余按日志补齐。

### D5 `execute_capability` 支持 `wait`

多步异步任务（报表最典型：建任务 → 轮询 → 取数）不适合让 agent 自己串：
忘了轮询、轮询过密、任务没完成就取数，都会失败。

方案是**在能力元数据里描述轮询协议**，执行器按元数据自动等：

```json
"async_task": {
  "status_capability": "OpenapiReportService.getTaskStatus",
  "result_capability": "OpenapiReportService.getReportData",
  "task_id_field": "taskId",
  "done_when": {"status": [3]},
  "poll_interval": 2.0,
  "timeout": 60.0
}
```

`execute_capability(..., wait=True)` 时：

1. 执行建任务能力，拿到 `task_id`
2. 按 `poll_interval` 轮询 `status_capability`，直到 `done_when` 命中或超时
3. 命中后若定义了 `result_capability`，再执行一次取结果
4. 超时返回 `{ok: true, data: {task_id, status}, meta: {timed_out: true}}`，**不抛错**——
   任务已经建了，报错会让调用方误以为要重试

没有 `async_task` 元数据的能力，`wait=True` 被忽略，返回值里说明原因。

## 备选方案

| 方案 | 否决理由 |
|---|---|
| 账户私有于用户 | 代运营场景下同一账户要重复授权，`refreshToken` 会被互相覆盖 |
| 只支持独立账户 | 百度 MCC 是常见形态，强制逐个授权不现实 |
| 依赖 Casdoor org claim 判租户 | 字段格式跨版本变动，`sub` 才是稳定契约 |
| 让 agent 自己轮询 | 实测最容易错的环节，且同一套轮询逻辑要在每个 skill 里重写 |
| `wait` 作为默认行为 | 长任务会占住工具调用，且多数能力根本不需要等 |

## 后果

### 正面

- 租户内账户可共享，代运营场景不用重复授权
- MCC 与独立账户统一建模，调用方只关心 alias
- `wait` 把"怎么轮询"收进服务端，skill 只管业务流程
- 权限档位与 `mutating` 挂钩，越权在能力层就能拦住

### 负面 / 需要承担

| 风险 | 说明 | 应对 |
|---|---|---|
| 表结构更复杂 | 五张表 + 两个唯一约束 | 一次性建对，后面少迁移 |
| MCC 上溯一层 | 取凭据要递归一层，出错时难排查 | 取凭据处打日志，标明 `account` / `parent` |
| `mutating=unknown` 收紧 | 权限不足的用户可能被误拒 | 先标 unknown 的接口做人工核查，逐步收敛 |
| `wait` 超时不报错 | 调用方可能以为完成 | 返回 `timed_out: true`，并在描述里写清 |

## 未决问题

- [ ] `mutating` 灰区（导出任务、审核提交）的裁决清单
- [ ] `wait` 的超时上限：60 秒是否够，报表可能要几分钟
- [ ] MCC 子账户的发现：是用户手工添加，还是调 `MccFeedService.getUserListByMccid` 自动拉取
- [ ] 租户自助注册流程：是否允许自助开户，还是运营后台创建
