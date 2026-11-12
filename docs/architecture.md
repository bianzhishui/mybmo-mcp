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
| IdP 里按组织隔离应用归属 | Casdoor 行为（应用只能登录同组织用户） | `built-in` 只服务 dbx-mcp/casdoor-mcp，`pandabase` 只服务本服务；组织在鉴权层校验（aud + owner），授权表不表达组织 |

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

**应用凭据跟着凭据走**，不是服务级全局配置（[ADR 0006](decisions/0006-multi-app-credentials.md)）：
一个百度账号下可以有多个应用，本项目限制「一个百度账户只绑一个应用」。
凭据存在 PostgreSQL（[ADR 0007](decisions/0007-credentials-in-postgres.md)），
`file` 后端保留给本地开发与单测。

| 数据 | 存放位置 | 生命周期 |
|---|---|---|
| `AppID` / `SecretKey` | `app_secrets` 表（整块 AES-GCM 密文）；密钥只经 CLI 隐藏输入 | 长期；**重置即让该应用下所有令牌失效**，等于一次全员重绑 |
| 百度 `refreshToken` / `accessToken` | `credentials.cipher`（密文）；明文列只放查询需要的字段 | refresh ~30 天且每次刷新轮换；access ~24 小时 |
| 账户中文名 `userName` | `credentials.user_name`（明文列） | 随账户；**后台改名后刷新时自动校正**，否则全部接口报 81020 |

绑定入口是 `GET /oauth/baidu/callback`：它是**两条免 Token 鉴权的业务路由**之一（另一条是
`GET/POST /oauth/baidu/subscribe`，百度事件推送）。回调的安全性由一次性 state（15 分钟 TTL，
取用即删）与"授权码只能换一次"共同保证，只写凭据、只回显别名与中文名，不回显任何令牌。

实测（2026-11-12）：回调会原样带回 `state`，并附带 `appId` / `userId` / `signature` / `timestamp`。
`appId` 让"state 丢了"时还能定位应用；`signature` 暂不校验（算法未公开，且伪造仍需有效授权码）。
`uvicorn` 的 access log 会打印 query，已用日志过滤器把 query 摘掉——否则授权码会进容器日志。

订阅端点只做**采样**：验共享密钥 → 脱敏后写入 `subscribe_events` 表 → 回 200。
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
  │ app secret set   │                                   │
  │─────────────────>│ 密钥只进 app_secrets（密文），不经对话
  │  auth bind       │                                   │
  │─────────────────>│ 按 appId 取密钥 → 预写 pending 占位凭据
  │<── 授权 URL ─────│ 生成一次性 state（落 binding_states，15 分钟 TTL）
  │ 浏览器打开 ────────────────────────────────────────>│
  │                  │<── 回调 authCode+state+appId+userId ─│
  │                  │ state → alias → 取回应用密钥
  │                  │ 换 accessToken/refreshToken
  │                  │ 调 getUserInfo 取账户中文名
  │                  │ 查 1:1 唯一性 → 回填 pending 行并置 active
  │<── 绑定成功 ─────│
```

要点：`state` 必须是一次性 nonce，不能是 `md5(appId_uid)` 这类可推导值；换令牌失败时
**不回显 authCode**；落库前必须拿到 `userName`，否则后续所有调用必失败。

**冲突在回调阶段才发现**：授权前拿不到 `baidu_user_id`，所以「该百度账户已绑过别的应用」只能
在换到令牌后判定。此时**丢弃令牌、不落库**并提示先 `auth unbind`，不做自动替换——静默替换会让
一组授权无声失效。

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

> **当前实现**：授权表与凭据都是**双后端**。
> 授权表默认文件式 `data/tenancy.json`，设 `MYBMO_TENANCY_BACKEND=pg` 走 PostgreSQL；
> 凭据默认加密文件 `data/accounts/*.json`，设 `MYBMO_CREDENTIAL_BACKEND=pg` 走
> PostgreSQL 的 `credentials` / `app_secrets` 表（生产即此配置）。
> 每类后端都是同一套协议的两个实现，调用方零改动。

```sql
tenants          (id, name, plan, status, client_id, created_at)
                                                      -- client_id 唯一：一个租户 = 一个 Casdoor 应用
users            (id, tenant_id, idp_subject, email, display_name,
                  status, created_at)                   -- 无 role：权限只来自 grants
baidu_accounts   (id, tenant_id, alias, owner_user_id, kind, baidu_user_id, user_name,
                  parent_account_id, app_id, access_token_expires_at,
                  status, created_at, updated_at)     -- 归属与展示，不存令牌
credentials      (tenant_id, alias, app_id, user_name, baidu_user_id, status,
                  expires_at, refresh_expires_at, is_default,
                  nonce, cipher, key_version, created_at, updated_at)
                                                      -- 凭据本体，见 migrations/003
app_secrets      (tenant_id, app_id, nonce, cipher, created_at, updated_at)
binding_states   (nonce PK, tenant_id, subject, alias, app_id, issued_at, expires_at)
                                                      -- 一次性绑定 state；DELETE … RETURNING 保证不重放
account_grants   (user_id, account_id, permission, created_at)  -- 只 read | write
audit_log        (id, call_id, started_at, finished_at, duration_ms, tenant_id,
                  user_sub, user_name, app, account_alias, account_name, capability,
                  state, http_status, error_code, error,
                  request_params, response_body, request_bytes, response_bytes,
                  payload_digest)                        -- 见 migrations/002_audit_log.sql

UNIQUE (tenant_id, alias)                              -- 别名在租户内唯一，跨租户可重名
UNIQUE (tenant_id, idp_subject)                        -- 一个 IdP 身份在一个租户内只对应一个用户
UNIQUE (tenant_id, baidu_user_id) WHERE baidu_user_id > 0   -- 一个百度账户只绑一个应用
UNIQUE (tenant_id, app_id) WHERE app_id <> ''               -- 一个应用只服务一个账户
```

`credentials` 与 `baidu_accounts` 分工：**前者是凭据本体**（密文 + 过期时间），
**后者是归属与展示**（谁绑定、中文名、用户名下的账户）。`baidu_accounts` 原有的
`secret_key_cipher` / `refresh_token_cipher` 两列从未写入过，已随 `003` 迁移删除。

账户形态：

| `kind` | 令牌 | 调用时的 `userName` | 当前状态 |
|---|---|---|---|
| `standalone` 独立账户 | 自己的 `app_id` + `refresh_token` | 该账户中文名 | ✅ 已实现 |
| `mcc_child` 管家子账户 | **复用** `parent_account_id` 的令牌 | **子账户**中文名（填错报 81020） | ⏸️ **本期不做**（ADR 0006 明确排除） |

`kind` / `parent_account_id` 两列保留为将来做 MCC 的位置，当前恒为 `standalone` / `NULL`。

**账户私有**：`owner_user_id` 是绑定者，owner 隐式拥有 admin；`account_grants` 只表达
`read` / `write`（admin 不能转授）。没有 `owner_user_id` 的账户对任何人不可见（fail closed）。

审计表**不建外键**：用户未登记、账户被删时审计仍要写得进去，所以 `user_sub` /
`account_alias` / `user_name` / `account_name` 都是冗余文本。详见 `docs/audit.md`。

多租户隔离原则：所有查询带 `tenant_id`，不靠应用层 if 判断；执行前必须经授权表校验，
不能直接信任入参 alias；`read` 只能调 `mutating=false` 的能力，`mutating=unknown` 按写处理。

## 五、凭据生命周期

```text
app secret set → 密钥进 app_secrets（密文）
   → auth bind → 预写 pending 占位凭据 → 签发 state
   → 回调 → 换令牌 → 查 1:1 唯一性 → 置 active
   → 调用前检查过期 → 剩余 < 5min（EXPIRY_SKEW=300s）触发刷新
   → 刷新：`SELECT … FOR UPDATE` 行锁串行 → 换令牌 → 顺手校正 user_name
   → 新令牌先落库再使用
   → 刷新失败：标记 reauth_required，通知用户重新绑定
```

红线：

- 刷新失败**不重试旧令牌**
- 并发刷新必须串行化：用 `SELECT … FOR UPDATE` 行锁，且**必须写回同一事务**
  （另开连接会等自己持有的锁而死锁）。文件锁已随 ADR 0009 删除
- **`secretKey` 重置会让该应用下所有令牌立即失效**（实测 `50003` / `89406`），
  运维上等于一次全员重绑，必须提前通知
- 日志、异常、审计中 `accessToken` / `refreshToken` / `SecretKey` 一律脱敏；
  access log 的 query 也要摘掉，否则 `authCode` 会落进容器日志

## 六、失败模式

| 故障 | 表现 | 处理 |
|---|---|---|
| IdP 不可用 | 所有请求 401 | 已签发令牌在有效期内仍可验证（JWKS 缓存 10 分钟）；详情见 infra 仓库 |
| 百度令牌过期 | 110 等鉴权错误 | 触发刷新；失败则 `reauth_required` |
| 账户中文名被改 | **81020**，但 `auth_status` 显示 active | 刷新时自动校正 `user_name`；仍失败则 `auth unbind` 重绑 |
| 应用密钥被重置 | 换令牌 `50003 token验签失败`、接口 `89406` | 用新密钥重绑该应用下所有账户 |
| 百度接口限流 | `rquota` 异常 | 读取配额头，退避；写操作不自动重试 |
| 网络超时（写） | 结果未知 | 先查后写，禁止重发 |
| 凭据记录损坏 / 解密失败 | 该账户不可用 | 明确报错，不静默忽略（文件后端逐文件、PG 后端逐行报告） |
| 唯一性冲突 | 绑定被拒 | 提示「该百度账户已绑定应用 X（别名 Y），先 `auth unbind Y`」 |


## 附：Docker 构建的两个排查动作

"改了 Dockerfile 但镜像里没变化"多半是**构建缓存**，不是写法错。按顺序验证，比换方案快：

```bash
# ① 单独构建依赖层，直接看 venv 里有没有目标包
docker build --target builder -t app-builder:test -f deploy/Dockerfile .
docker run --rm app-builder:test sh -c "ls /app/.venv/lib/python3.12/site-packages | grep -i <包名>"

# ② 确认不是缓存：强制无缓存重建（慢，但确定性高）
docker compose build --no-cache <service>
```

实例（2026-11-11）：`uv sync --extra db` 一度被误判为"不装 psycopg"，
实际是复用了旧缓存层；`--target builder` 一验就发现 psycopg 本来就在。
