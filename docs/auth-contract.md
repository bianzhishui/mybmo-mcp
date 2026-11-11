# 鉴权契约

本文件是 `mybmo-mcp` 与 `pandabase-infra` 之间的**代码级契约**。基础设施（Casdoor、PostgreSQL、
Caddy）的部署与运维在 infra 仓库，这里只记录**本仓库代码依赖的那些事实**。
改动本文件里的任何一条，都要同步通知 infra。

## 一、五个不能违背的不变量

1. **`aud` 是 client_id，不是资源地址。** Casdoor 把令牌的 `aud` 填成签发它的客户端
   client_id，与 RFC 8707 的资源标识语义不同。所以本服务把 `MYBMO_OIDC_AUDIENCE`
   当**白名单**用：每个允许调用它的客户端，把 client_id 加进去。
2. **JWKS 路径必须显式配置。** 实测在 `/.well-known/jwks`，不是常见的
   `{issuer}/api/v1/jwks`。推导值恰好能用是巧合，不是契约。
3. **不开启 DCR。** 客户端逐个在 Casdoor 显式注册。DCR 没有回调白名单与限流，
   且它签出的令牌 `aud` 是新 client_id，本来也进不了白名单。
4. **scope 在 Casdoor 里是对象数组**（`[{"name":"baidu:read",...}]`），写成字符串数组会
   被拒。本服务只认 `baidu:read` / `baidu:write` / `baidu:admin`。
5. **多用户模式下不得回退成本地身份。** 拿不到身份就报错，绝不把「本机」当管理员。
6. **授权按组织隔离，不跨组织关联。** Casdoor 有两个组织：`built-in` 只服务
   `dbx-mcp` / `casdoor-mcp`，`pandabase` 只服务 `mybmo-mcp`。授权表 v2 的
   `grants[].org` 必须等于令牌的 `owner`（组织），跨组织的 grant 一律不可见；
   同一组织内可用 `grants[].app`（client_id）再收窄到具体应用。

## 二、环境变量（值由 infra 提供，变量名由本仓库决定）

| 变量 | 含义 | 缺失时 |
|---|---|---|
| `MYBMO_AUTH_MODE` | `local`（stdio 单用户）/ `oidc`（校验 JWT） | 默认 `local` |
| `CASDOOR_ISSUER` | 令牌 `iss`，必须精确匹配 | oidc 模式拒绝启动 |
| `MYBMO_OIDC_AUDIENCE` | 允许的 client_id 白名单，逗号分隔 | oidc 模式拒绝启动 |
| `MYBMO_JWKS_URL` | JWKS 地址 | 回落到 `{issuer}/.well-known/jwks`（不推荐） |
| `MYBMO_RESOURCE_URL` | 元数据里的 `resource` 字段 | oidc 模式拒绝启动 |
| `MYBMO_BAIDU_CALLBACK_URL` | 百度 OAuth 授权回调（`auth bind` 用） | 只影响绑定；缺失时报错，不影响检索与执行 |
| `MYBMO_BAIDU_SUBSCRIBE_SECRET` | 百度订阅推送的共享密钥 | `GET/POST /oauth/baidu/subscribe` 返回 503（失败关闭） |
| `MYBMO_DATA_DIR` | 凭据、审计、锁、`tenancy.json` 的根 | 默认 `./data` |
| `MYBMO_MASTER_KEY` | base64 的 32 字节 AES-GCM 主密钥 | 能否启动不报错，但调不动百度 |
| `MYBMO_DATABASE_URL` | 平台 PG 的 DSN（当前未启用） | 忽略 |

## 三、端点契约

| 端点 | 约定 |
|---|---|
| `GET /healthz` | 不需要鉴权；**即使 `credentials=unconfigured` 也返回 200**，用字段表达降级，不靠状态码。编排与监控依赖这一点 |
| `POST /mcp` | 需要 Bearer 令牌；未认证返回 401 且带 `WWW-Authenticate` 与 `resource_metadata` |
| `GET /oauth/baidu/callback` | **百度授权回调，不需要本服务令牌**——跳转过来的是浏览器，带不了 Bearer。安全性靠一次性 state + 15 分钟 TTL + "授权码只能换一次"。反代必须把这个路径也转到容器 |
| `GET/POST /oauth/baidu/subscribe` | **百度事件推送接收端，不需要本服务令牌**，靠共享密钥（`MYBMO_BAIDU_SUBSCRIBE_SECRET`）把关：未配 → 503，验不过 → 401 且不落盘。命中后**只打印并追加到 `data/subscribe/YYYY-MM-DD.jsonl`，不解析成任何业务动作**（ADR 0003） |
| `/.well-known/oauth-protected-resource` | 由 MCP SDK 发布，不需要自己实现 |

反代（Caddy）**不做鉴权**：它注入的身份头可被直连回环伪造，令牌校验必须在本进程内完成。

## 四、数据卷布局

挂载点是 `/data`，由 infra 提供卷；里面放什么都由本仓库决定：

```
/data/tenancy.json     授权表 v2（按 org + sub 定位，可选 app；尚未落库）
/data/audit/*.jsonl    按天分文件的审计
/data/subscribe/*.jsonl 百度事件推送的原始样本（脱敏后），只供人工分析
/data/locks/           凭据刷新的文件锁
/data/<加密凭据文件>    AES-GCM 密文，权限 600
```

## 五、当前实例的取值（2026-11-08）

```text
issuer   https://auth.pandabase.top
jwks     https://auth.pandabase.top/.well-known/jwks
resource https://baiduads.pandabase.top
callback https://baiduads.pandabase.top/oauth/baidu/callback

# OAuth 客户端（每个 client_id 都必须进 MYBMO_OIDC_AUDIENCE 白名单）
mybmo-mcp    / 709a8e16656a223a1ab7   Casdoor 控制台登录 + 客户端验证（TTL 168h，无 refresh 到期）
mybmo-client / 06fa59d425b54981e537   备用客户端应用（TTL 1h + refresh 30 天），回调 127.0.0.1:33418
```

**Casdoor 的两个约束（都实测踩过）**：

1. **应用只能登录同组织的用户**。应用挂在 `pandabase` 组织下时，`built-in` 组织的 `admin`
   登录会报 `用户: pandabase/admin不存在`。当前可登录的是 `pandabase` 里的 `test01` 等账号。
2. **`refresh_expire_in_hours=0` 不等于没有 refresh token**。实测令牌里带 `refresh_token`，
   用 `/api/login/oauth/refresh_token` 能换到新令牌（`expires_in=604800`）。
   但 Casdoor 默认**开启 refresh 轮换**，新令牌会换掉旧的 refresh_token——所以刷新后必须
   立即落库（`auth/service.py` 的"先 save 再返回"就是为这个）。

`callback` 是**百度** OAuth 的回调，与上面 IdP 的凭据无关：它只是本服务对百度暴露的一个
路径。百度侧按**根域**比对（不一致报 `redirect_uri_mismatch`），所以换路径可以、换域名不行。

应用在 Casdoor 里只开 `authorization_code` + `refresh_token`；`redirect_uris` 按客户端逐个登记。
