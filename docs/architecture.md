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
| Casdoor DCR 默认关闭 | Casdoor 设计 | 上线前必须按组织开启并加白名单 |

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

### 3.2 普通用户调用只读工具

```text
用户 → MCP 客户端 → [Casdoor 登录] → Bearer JWT
                  → mybmo-mcp
                       ├ 校验 JWT
                       ├ sub + tenant → 查 account_grants
                       ├ 选账户（模型只能从返回列表里选）
                       ├ 取 refreshToken → 必要时刷新 → 注入 header
                       └ 调百度 → 归一化结果 → 返回
```

### 3.3 写操作

```text
用户 → change_preview(account, 意图)
     ← 完整报文 + 影响条数 + approval_id（TTL 5 分钟，单次使用）
用户 → change_execute(approval_id)
     → 服务端复核：payload_hash 一致、未使用、未过期、账户属于该租户
     → 发送 → 记审计（attempted / succeeded / failed / unknown）
```

## 四、数据模型

```sql
users            (id, idp_subject, email, status, created_at)
tenants          (id, name, plan, created_at)
baidu_accounts   (id, tenant_id, alias, baidu_user_id, user_name,
                  app_id, secret_key_cipher, refresh_token_cipher,
                  access_token_expires_at, status, created_at)
account_grants   (user_id, account_id, permission)   -- read | write | admin
approvals        (id, user_id, account_id, payload_hash,
                  scope, expires_at, used_at)
audit_log        (id, tenant_id, account_id, user_id, tool,
                  request_id, state, payload_digest, error, created_at)
```

多租户隔离原则：

- 所有查询带 `tenant_id`，不靠应用层 if 判断
- `account_id` 必须经 `account_grants` 校验，不能直接信任工具入参
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
- 开启组织级 DCR（`dcrPolicy`），并限制回调 URI 白名单
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
- [ ] DCR 开启后限制注册来源，避免任意注册
- [ ] 管理台不对公网暴露
- [ ] 写操作需 approval_id，且单次使用
- [ ] 审计记录可追溯到 `sub` + `tenant` + `account`
- [ ] 依赖定期扫描，`baiduads-sdk` 传递依赖需单独审查
