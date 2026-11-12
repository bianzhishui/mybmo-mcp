# ADR 0008：租户按 Casdoor `client_id` 解析，首次访问自动开户

- 状态：已接受
- 日期：2026-11-12
- 决策者：项目负责人
- 关联：[ADR 0004](0004-multi-tenant-accounts.md)（多租户账户模型）、
  [ADR 0006](0006-multi-app-credentials.md)（应用凭据）、
  [ADR 0007](0007-credentials-in-postgres.md)（凭据落库）

## 背景

ADR 0004 定的模型是「租户 → 用户 → 账户」，但实现一直停在**单租户**：
`PgTenancyStore` / `PgCredentialStore` / `PgAppSecretStore` 都是进程级单例，
构造时吃一个固定的 `MYBMO_TENANT_ID`（默认 `t_default`）。`Principal.tenant_id`
恒等于这个配置值，令牌里的 `org`（Casdoor owner）只用于组织白名单，**不参与租户判定**。

结果：库里 schema 是多租户的，运行时只有 `t_default` 一个租户。

现在的要求：**一个 Casdoor `client_id` 对应一个租户**，租户由令牌解析，而不是配置。

## 决策

### D1 `tenants` 加一列，不新建映射表

```sql
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS client_id text;
CREATE UNIQUE INDEX idx_tenants_client_id ON tenants (client_id);
```

一个租户只有一个 client_id、一个 client_id 也只属于一个租户（唯一索引即 1:1），
单独一张映射表只是把同一件事拆到两处。

**不用部分索引**（`WHERE client_id IS NOT NULL`）：`INSERT … ON CONFLICT (client_id)`
只能推断完整唯一索引，而 SQL 标准下唯一索引本来就允许多个 NULL——正好满足
"还没绑定的租户可以有多条"。

### D2 解析顺序

```text
① 验签（签名 / iss / exp）
② 取 client_id：azp 优先，退回 aud 的唯一值；都没有 → 401
③ tenants WHERE client_id = ?  → 命中即用
   未命中 → 自动开户：
       id   = 't_' + uuid4().hex[:12]
       name = '租户-' + id 后 4 位        （可读即可，之后 tenant rename 改）
       INSERT … ON CONFLICT (client_id) DO NOTHING，再回查（并发首请求只建一条）
④ resolve_principal(tenant, sub) → users 行（(tenant_id, idp_subject) 唯一）
```

**顺带修掉一个坑**：`resolve_principal` 在"用户行不存在"时原本返回 `tenant_id=""`，
多租户下必须返回**已解析出的租户**，否则新租户的第一个请求会拿着空 tenant 查库。
用户行仍不自动创建——"登录成功但没有账户"是合法状态。

### D3 `aud` 从白名单改为"令牌必须是发给这个 client 的"

自动开户要求"没见过的 client 也能通过验签"，所以静态白名单不能当闸门：

- PyJWT 的 `verify_aud` 关掉，改为手工规则：**`aud` 必须包含该令牌自己的 `client_id`**
  （即确认令牌是发给这个客户端，而不是别的资源）
- `MYBMO_OIDC_AUDIENCE` 退化为**可选**收紧项：设了就额外要求命中
- `MYBMO_OIDC_ORGS` 成为**唯一的上游准入闸门**

**代价写在明处**：pandabase 组织内任何 Casdoor 应用，拿着自己用户的令牌就能自助开租户。
这正式回答了 ADR 0004 的未决问题「是否允许自助开户」——**允许**。想收紧就把
`MYBMO_OIDC_AUDIENCE` 配成白名单，等于回到"必须先登记"。

### D4 存储按租户分实例

新增 `TenantContext(tenant_id, store, tenancy, app_secrets, registry, access, credentials)`，
由 `Runtime.context(principal)` 按租户构造并缓存。

- **删掉** `Runtime.store / tenancy / access / app_secrets / credentials` 这些无租户属性：
  漏改的调用点立刻 `AttributeError`，而不是静默读写别的租户
- PG 三个 store 的 `tenant_id` **去掉默认值**；`DEFAULT_TENANT` 常量删除
- **刻意不用 contextvar 隐式传递**：隐式租户漏设时不会报错，只会串数据

### D5 绑定与回调：`state` 是租户的唯一来源

回调那一跳来自浏览器，没有令牌可依据，只有 `state`：

- `state` 缺失 / 过期 / 被重放 → **直接失败**，提示重新发起绑定
- **删除** appId 兜底与 `acct_<userId>` 命名（多租户下 appId 可能被多个租户登记，
  取不到唯一租户；猜一个等于把凭据写进错租户且事后无法识别）
- `auth bind --tenant` 在多租户下必填；MCP 的 `bind_account` 用 `principal.tenant_id`

### D6 CLI

```bash
mybmo-mcp tenant list                        # id / name / client_id / 用户数 / 账户数
mybmo-mcp tenant set-client <tenant> <cid>   # 绑定或修复（存量租户上线时用）
mybmo-mcp tenant rename <tenant> <name>      # 自动名是"租户-xxxx"，可读性靠它补
```

**没有 `tenant create`**：租户由 client_id 首次出现决定，手工建租户没有意义。

### D7 启动自检与探活

- `preflight()`：pg 模式下每个租户都必须有 client_id，缺了**拒绝启动**并列出。
  否则表现成"服务起来了但所有请求 403"，像鉴权坏了
- `/healthz` 的租户/用户/账户计数：pg 走 `tenants` 汇总，file 走 `tenancy.json`

### D8 `file` 后端不表达租户

`file` 后端（`data/tenancy.json` + `data/accounts/*.json`）没有租户维度，
多租户配置下它退回单租户（取 `MYBMO_TENANT_ID`）。它只服务 CLI 与测试，
**后续整体移除**。

## 备选方案

| 方案 | 否决理由 |
|---|---|
| 新建 `tenant_clients` 映射表 | 1:1 关系用一列足够；拆表只会让"谁是权威"多一处 |
| 保留 aud 白名单当准入闸门 | 那么新增租户必须先改环境变量 + 重启，做不到"新租户进来就用" |
| 用 contextvar 隐式传租户 | 漏设不报错，只会静默跨租户读写——最坏的一类 bug |
| 保持 `MYBMO_TENANT_ID` 单租户 | 就是当前状态，达不到需求 |
| 回调 state 缺失时按 appId 兜底 | 多租户下 appId 可能不唯一，猜租户等于污染数据 |

## 后果

### 正面

- 加一个新租户 = 让它的 Casdoor 应用发起一次请求，**无需改配置、无需重启**
- 隔离靠实例化边界（每个租户一套 store），而不是靠每个查询记得写 `WHERE tenant_id`
- 无租户属性的 API 被删掉，漏改会立刻崩

### 负面 / 需要承担

| 风险 | 说明 | 应对 |
|---|---|---|
| 自助开户可被组织内任何应用使用 | 上游只剩组织白名单 | 想收紧：配 `MYBMO_OIDC_AUDIENCE` |
| state 丢失即绑定失败 | 用户需要重来一次 | 实测百度会回传 state；文案写清"重新发起" |
| 租户数量无上限 | 每个租户会在进程里缓存一套 store | 观察租户数；必要时加淘汰策略 |
| 存量租户要手工绑一次 client_id | 忘了就启动失败 | 启动自检直接报错并给出修复命令 |

## 未决问题

- [ ] 租户级配额/限流（现在所有租户共用进程级限速）
- [ ] 删除租户的流程：账户、凭据、审计怎么处理
- [ ] 账户在租户之间迁移（现在只能在新租户重新绑定）
- [ ] 租户数增长后的 context 缓存淘汰
