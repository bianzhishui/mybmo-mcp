# 架构设计

本文描述 mybmo-mcp 的信任边界、数据模型与运行拓扑。
决策依据见 [`decisions/0001-auth-architecture.md`](decisions/0001-auth-architecture.md)。

## 一、设计约束

| 约束 | 来源 | 影响 |
|---|---|---|
| 百度令牌按账户发放，刷新会被轮换 | 百度 OAuth | 必须按账户隔离存储，刷新加锁 |
| `userName` 必须是令牌所属账户中文名 | 百度接口要求（错误码 81020） | 账户名不能靠猜，必须授权后落库 |
| 百度自定义 state 是否原样回传未实证 | 百度 OAuth 行为 | 回调必须能退化为按 userId 命名 |
| MCP 规范要求校验 token audience | MCP 2026-07-28 | 不能把百度令牌当接入令牌 |
| Casdoor DCR 不可用 | 实测（ADR 0007） | 无回调白名单、无限流、aud 不匹配；客户端改为显式注册 |

## 二、三层信任边界

### 第一层：MCP 客户端 → mybmo-mcp

mybmo-mcp 是 **OAuth 2.1 Resource Server**，不是授权服务器。

必须发布：

```http
GET /.well-known/oauth-protected-resource
{
  "resource": "https://mcp.example.com",
  "authorization_servers": ["https://auth.example.com"],
  "scopes_supported": ["baidu:read", "baidu:write", "baidu:admin"]
}
```

未认证请求返回：

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"
```

每个请求必须校验：

| 项 | 校验方式 | 失败处理 |
|---|---|---|
| 签名 | Casdoor JWKS | 401 |
| `iss` | 等于配置的 Casdoor issuer | 401 |
| `aud` | 等于本服务规范地址 | 401 |
| `exp` / `nbf` | 时钟偏移容忍 ≤ 60s | 401 |
| `scope` | 按工具要求 | 403 + `insufficient_scope` |

**禁止**：接受未在 Casdoor 注册的 issuer 签发的令牌；把百度令牌当 Bearer 使用。

### 第二层：mybmo-mcp → 百度

这不是用户登录，是**账户绑定**。服务端持有应用级凭据和账户级令牌。

| 数据 | 存放位置 | 生命周期 |
|---|---|---|
| `AppID` / `SecretKey` | 环境变量 + KMS / Secrets Manager | 长期，轮换需人工 |
| 百度 `refreshToken` | 凭据库，按租户信封加密 | ~30 天，刷新可能轮换 |
| 百度 `accessToken` | 内存或 Redis | ~24 小时 |
| 账户中文名 `userName` | 凭据库明文（非敏感） | 随账户 |

### 第三层：写操作（已移出本项目）

本层原本设计为 `change_preview → 用户确认 → change_execute` 的审批闭环。

按 [ADR 0003](decisions/0003-capability-only.md) 的方向调整，**审批不再由本服务承担**：
MCP 只忠实执行 `execute_capability`，是否要求人工确认由调用方（skill 工作流）决定。
能力元数据里会标 `mutating` / `destructive`，把判断所需的事实给足，但不替调用方做决定。

仍然保留的约束：**写请求不自动重试**。网络超时属于"结果未知"，必须先查询后台状态，
禁止直接重发——这是可靠性要求，与审批无关。

## 三、关键时序

### 3.1 管理员首次接入百度账户

```text
管理员            mybmo-mcp              Casdoor            百度 OAuth
  │  auth bind       │                     │                   │
  │─────────────────>│ 校验已有会话(scope=admin)                │
  │                  │ 生成一次性 state(nonce, 绑定 sub+租户+exp)
  │<── 授权 URL ─────│                     │                   │
  │ 浏览器打开 ───────────────────────────────────────────────>│
  │                  │<──────── 回调 authCode + state ──────────│
  │                  │ 校验 state → 换 accessToken/refreshToken │
  │                  │ 调 getUserInfo 取账户中文名               │
  │                  │ 加密落库 baidu_accounts                  │
  │<── 绑定成功 ─────│                     │                   │
```

要点：

- `state` 必须是一次性 nonce，不能是 `md5(appId_uid)` 这类可推导值
- 换令牌失败时**不回显 authCode**
- 落库前必须拿到 `userName`，否则后续所有调用必失败

### 3.2 普通用户调用只读能力

```text
用户 → MCP 客户端 → [Casdoor 登录] → Bearer JWT
                  → mybmo-mcp
                       ├ 校验 JWT
                       ├ sub + tenant → 查 account_grants
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

本服务**不判断"这次操作该不该执行"**。写操作是否需要人工确认，由 skill 工作流决定
（见 [ADR 0003](decisions/0003-capability-only.md)）。能力元数据只负责把
`mutating` / `destructive` 这些事实给足。

## 四、数据模型

详见 [ADR 0004](decisions/0004-multi-tenant-accounts.md)。

> **现状**：这些表还没建。P4 只落地了 JWT → `Principal` 的校验，账户授权仍是文件式
> `data/tenancy.json`，`psycopg` 还是 `optional-dependencies.p4` 里未启用的依赖。
> 本节是目标模型，不是当前库结构。

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
UNIQUE (tenant_id, idp_subject)   -- 一个 Casdoor 身份在一个租户内只对应一个用户
```

账户两种形态：

| `kind` | 令牌 | 调用时的 `userName` |
|---|---|---|
| `standalone` 独立账户 | 自己的 `app_id` + `refresh_token` | 该账户中文名 |
| `mcc_child` 管家子账户 | **复用** `parent_account_id` 的令牌 | **子账户**中文名（填错报 81020） |

`mcc_child` 不存令牌，取凭据时沿 `parent_account_id` 上溯一层。

多租户隔离原则：

- 所有查询带 `tenant_id`，不靠应用层 if 判断
- 执行前必须经 `account_grants` 校验，不能直接信任入参 alias
- 权限档位：`read` 只能调 `mutating=false` 的能力，`write` 可调写，`admin` 另可管理授权
- `mutating=unknown` 的能力按写处理（收紧而非放开）
- 凭据表每个租户独立 DEK（信封加密），主密钥在 KMS

## 五、凭据生命周期

```text
绑定 → 加密写入 → 调用前检查过期 → 剩余 < 10min 触发刷新
     → 刷新加分布式锁（防轮换丢失）→ 新令牌先落库再使用
     → 刷新失败：标记 reauth_required，通知用户重新绑定
```

红线：

- 刷新失败**不重试旧令牌**
- 并发刷新必须串行化，否则轮换后旧 refreshToken 失效，账户直接掉线
- 日志、异常、审计中 `accessToken` / `refreshToken` / `SecretKey` 一律脱敏

## 六、部署拓扑

```text
            互联网
              │ HTTPS
      ┌───────▼────────┐
      │ Caddy / Nginx  │  自动证书
      └───┬────────┬───┘
          │        │
   auth.example.com  mcp.example.com
          │        │
    ┌─────▼─────┐  ┌▼──────────────┐
    │ Casdoor   │  │ mybmo-mcp     │
    │ :8000     │  │ :8080         │
    └─────┬─────┘  └┬──────────────┘
          │         │
    ┌─────▼─────────▼─────┐     ┌──────────────┐
    │ PostgreSQL          │     │ KMS / Secrets│
    └─────────────────────┘     └──────────────┘
```

规格建议（够用即可）：

| 组件 | 规格 | 说明 |
|---|---|---|
| 腾讯云轻量 | 2核2G，¥45/月 | Casdoor + server + PG 同机，用户 < 200 |
| 磁盘 | 40G SSD | 凭据与审计增长很慢 |
| 地域 | 中国香港 / 新加坡 | 免备案；百度 API 为服务端出站，不受地域影响 |
| 备份 | PG 每日 `pg_dump` | 凭据丢失等于所有账户掉线 |

Casdoor 配置要点：

- 管理台（:8000）**不对公网开放**，只走 SSH 隧道或内网
- 首次登录立刻改掉 `admin/123`
- **保持 DCR 关闭**（[ADR 0007](decisions/0007-no-dcr.md)），客户端逐个显式注册：
  建应用 → 填该客户端固定回调 → clientId 加进 `MYBMO_OIDC_AUDIENCE`
- 确认 discovery 中 `registration_endpoint` 与 `client_id_metadata_document_supported` 的实际取值
- `issuer` 绑定域名，**第一天上错后面全废**

## 七、失败模式

| 故障 | 表现 | 处理 |
|---|---|---|
| Casdoor 不可用 | 所有请求 401 | 已签发令牌在有效期内仍可用；缩短依赖链，不缓存鉴权结果过久 |
| 百度令牌过期 | 81020 / 110 错误 | 触发刷新；失败则 `reauth_required` |
| 百度接口限流 | `rquota` 异常 | 读取配额头，退避；写操作不自动重试 |
| 网络超时（写） | 结果未知 | 先查后写，禁止重发 |
| 凭据文件损坏 | 账户不可用 | 明确报错，不静默忽略 |

## 八、安全清单（上线前逐项确认）

- [ ] 所有 HTTP 强制 HTTPS，HSTS 开启
- [ ] JWT 必须校验 `aud`，不接受跨服务令牌
- [ ] 百度令牌不出服务端，不进日志、不进模型上下文
- [ ] 回调 state 一次性、绑定用户与租户、有 TTL
- [ ] DCR 保持关闭，客户端逐个显式注册（已决定，见 ADR 0007）
- [ ] 管理台不对公网暴露
- [ ] 账户归属校验生效：alias 不在 `account_grants` 内直接拒绝
- [ ] 审计记录可追溯到 `sub` + `tenant` + `account`
- [ ] 依赖定期扫描，`baiduads-sdk` 传递依赖需单独审查
