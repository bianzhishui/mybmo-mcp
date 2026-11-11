# 架构设计

本文只描述**业务侧（数据面）**：本服务怎么校验身份、怎么拿百度令牌、怎么调接口、数据怎么存。
部署拓扑、Casdoor、PostgreSQL、Caddy 与备份在 [`pandabase-infra`](https://github.com/bianzhishui/pandabase-infra)。
代码依赖的鉴权事实见 [`auth-contract.md`](auth-contract.md)。

## 一、设计约束

| 约束 | 来源 | 影响 |
|---|---|---|
| 百度令牌按账户发放，刷新会被轮换 | 百度 OAuth | 必须按账户隔离存储，刷新加锁 |
| `userName` 必须是令牌所属账户中文名 | 百度接口要求（错误码 81020） | 账户名不能靠猜，必须授权后落库 |
| 百度自定义 state 是否原样回传未实证 | 百度 OAuth 行为 | 回调必须能退化为按 userId 命名 |
| 百度按**根域**比对回调地址 | 百度 OAuth（`redirect_uri_mismatch`） | 回调路径可换，域名不能换 |
| MCP 规范要求校验 token audience | MCP 2026-07-28 | 不能把百度令牌当接入令牌 |
| 令牌由外部 IdP 签发，`aud` 是 client_id | 实测（infra 仓库 ADR 0003） | 本服务把 audience 当白名单用 |
| IdP 里按组织隔离应用归属 | Casdoor 行为（应用只能登录同组织用户） | `built-in` 只服务 dbx-mcp/casdoor-mcp，`pandabase` 只服务本服务；授权表按 `org` 隔离，不跨组织关联 |

## 二、三层信任边界

### 第一层：MCP 客户端 → 本服务

本服务是 **OAuth 2.1 Resource Server**，不签发令牌。每个请求校验五件事：

| 项 | 校验方式 | 失败处理 |
|---|---|---|
| 签名 | issuer 的 JWKS（按 `kid` 查，缓存 10 分钟） | 401 |
| `iss` | 与配置精确字符串比较 | 401 |
| `aud` | 必须命中 client_id 白名单 | 401 |
| `exp` / `nbf` | 时钟偏移容忍 ≤ 60s | 401 |
| `scope` | 按工具要求 | 403 + `insufficient_scope` |

两条不能妥协：**不接受未声明的 issuer**；**不把百度 accessToken 当 Bearer 用**。
JWKS 拉取失败时拒绝而非放行——故障期间"先信旧密钥"等于接受任何人的令牌。

未认证请求返回 401，并带上指向 `/.well-known/oauth-protected-resource` 的 `WWW-Authenticate`
（由 MCP SDK 发布）。

### 第二层：本服务 → 百度

这不是用户登录，是**账户绑定**。服务端持有应用级凭据和账户级令牌。

| 数据 | 存放位置 | 生命周期 |
|---|---|---|
| `AppID` / `SecretKey` | 环境变量 | 长期，轮换需人工 |
| 百度 `refreshToken` | 凭据文件，按租户信封加密 | ~30 天，刷新可能轮换 |
| 百度 `accessToken` | 内存 | ~24 小时 |
| 账户中文名 `userName` | 凭据文件明文（非敏感） | 随账户 |

绑定入口是 `GET /oauth/baidu/callback`：它是**两条免 Token 鉴权的业务路由**之一（另一条是
`GET/POST /oauth/baidu/subscribe`，百度事件推送）。回调的安全性由一次性 state（15 分钟 TTL，
取用即删）与"授权码只能换一次"共同保证，只写凭据、只回显别名与中文名，不回显任何令牌。

订阅端点只做**采样**：验共享密钥 → 打印并追加到 `data/subscribe/*.jsonl` → 回 200。
不解析成任何业务动作——把公网推送直接翻译成"暂停计划/调预算"，等于绕开 ADR 0003 开了个写入口。

### 第三层：写操作

**审批不由本服务承担**（[ADR 0003](decisions/0003-capability-only.md)）：MCP 只忠实执行
`execute_capability`，是否要求人工确认由调用方的工作流决定。能力元数据里标
`mutating` / `destructive`，把判断所需的事实给足，但不替调用方做决定。

写请求**不自动重试**：网络超时属于"结果未知"，必须先查后台状态，禁止直接重发。

## 三、关键时序

### 3.1 管理员首次接入百度账户

```text
管理员            mybmo-mcp                          百度 OAuth
  │  auth bind       │                                   │
  │─────────────────>│ 生成一次性 state(nonce, 绑定 sub+租户+exp)
  │<── 授权 URL ─────│                                   │
  │ 浏览器打开 ────────────────────────────────────────>│
  │                  │<──────── 回调 authCode + state ────│
  │                  │ 校验 state → 换 accessToken/refreshToken
  │                  │ 调 getUserInfo 取账户中文名
  │                  │ 加密写入凭据库
  │<── 绑定成功 ─────│
```

要点：`state` 必须是一次性 nonce，不能是 `md5(appId_uid)` 这类可推导值；换令牌失败时
**不回显 authCode**；落库前必须拿到 `userName`，否则后续所有调用必失败。

### 3.2 普通用户调用只读能力

```text
用户 → MCP 客户端 → [IdP 登录] → Bearer JWT
                  → mybmo-mcp
                       ├ 校验 JWT（签名/iss/aud/exp/scope）
                       ├ sub + tenant → 查授权表
                       ├ 选账户（模型只能从返回列表里选）
                       ├ 取 refreshToken → 必要时刷新 → 注入 header
                       └ 调百度 → 归一化结果 → 返回
```

### 3.3 能力调用（含写操作）

```text
用户 → search_capabilities(意图)
     ← 候选能力短摘要（key / 一句话 / mutating）
用户 → get_capability_schema(capability)
     ← 完整参数定义 + 字段说明
用户 → execute_capability(capability, account, input)
     → 校验账户归属 → 取令牌（必要时刷新）→ 执行 → 记审计
```

本服务**不判断"这次操作该不该执行"**，只负责把 `mutating` / `destructive` 这些事实给足。

## 四、数据模型

详见 [ADR 0004](decisions/0004-multi-tenant-accounts.md)。

> **当前实现**：账户授权是文件式 `data/tenancy.json`；`psycopg` 仍是
> `optional-dependencies.db` 里未启用的依赖。下面的表是目标模型，尚未建库。

```sql
tenants          (id, name, plan, status, created_at)
users            (id, tenant_id, idp_subject, email, display_name,
                  role, status, created_at)              -- owner | admin | member
baidu_accounts   (id, tenant_id, alias, kind, baidu_user_id, user_name,
                  parent_account_id, app_id, secret_key_cipher,
                  refresh_token_cipher, access_token_expires_at,
                  status, created_at, updated_at)
account_grants   (user_id, account_id, permission, created_at)  -- read | write | admin
audit_log        (id, tenant_id, account_id, user_id, capability,
                  request_id, state, payload_digest, error, created_at)

UNIQUE (tenant_id, alias)         -- 别名在租户内唯一，跨租户可重名
UNIQUE (tenant_id, idp_subject)   -- 一个 IdP 身份在一个租户内只对应一个用户
```

账户两种形态：

| `kind` | 令牌 | 调用时的 `userName` |
|---|---|---|
| `standalone` 独立账户 | 自己的 `app_id` + `refresh_token` | 该账户中文名 |
| `mcc_child` 管家子账户 | **复用** `parent_account_id` 的令牌 | **子账户**中文名（填错报 81020） |

`mcc_child` 不存令牌，取凭据时沿 `parent_account_id` 上溯一层。

多租户隔离原则：所有查询带 `tenant_id`，不靠应用层 if 判断；执行前必须经授权表校验，
不能直接信任入参 alias；`read` 只能调 `mutating=false` 的能力，`mutating=unknown` 按写处理。

## 五、凭据生命周期

```text
绑定 → 加密写入 → 调用前检查过期 → 剩余 < 10min 触发刷新
     → 刷新加锁（防轮换丢失）→ 新令牌先落库再使用
     → 刷新失败：标记 reauth_required，通知用户重新绑定
```

红线：

- 刷新失败**不重试旧令牌**
- 并发刷新必须串行化，否则轮换后旧 refreshToken 失效，账户直接掉线
- 日志、异常、审计中 `accessToken` / `refreshToken` / `SecretKey` 一律脱敏

## 六、失败模式

| 故障 | 表现 | 处理 |
|---|---|---|
| IdP 不可用 | 所有请求 401 | 已签发令牌在有效期内仍可验证（JWKS 缓存 10 分钟）；详情见 infra 仓库 |
| 百度令牌过期 | 81020 / 110 错误 | 触发刷新；失败则 `reauth_required` |
| 百度接口限流 | `rquota` 异常 | 读取配额头，退避；写操作不自动重试 |
| 网络超时（写） | 结果未知 | 先查后写，禁止重发 |
| 凭据文件损坏 | 账户不可用 | 明确报错，不静默忽略 |
