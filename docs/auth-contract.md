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
3. **DCR 已关闭（组织级 `dcrPolicy` 置空 = disabled）。** 实测证据：向
   `/api/oauth/register` 发注册请求返回 `400 invalid_client_metadata`
   「dynamic client registration is disabled for this organization」。
   DCR 没有回调白名单与限流，且它签出的令牌 `aud` 是新 client_id，本来也进不了白名单
   ——所以它只会在 Casdoor 里堆垃圾应用（历史上堆过 4 个 `dcr_*`，已清理）。
4. **scope 在 Casdoor 里是对象数组**（`[{"name":"baidu:read",...}]`），写成字符串数组会
   被拒。本服务只认 `baidu:read` / `baidu:write` / `baidu:admin`。
5. **多用户模式下不得回退成本地身份。** 拿不到身份就报错，绝不把「本机」当管理员。
6. **只有一个应用、一个组织。** 本服务只接受 Casdoor 的 `pandabase` 组织下
   `mybmo-mcp` 应用（client_id `709a8e16656a223a1ab7`）签出的令牌：`MYBMO_OIDC_AUDIENCE`
   里只放这一个 client_id，`MYBMO_OIDC_ORGS=pandabase`。多出来的客户端应用一律删除，
   见[第二层](#二环境变量值由-infra-提供变量名由本仓库决定)那两条变量。
7. **组织边界由鉴权层保证，授权表不重复表达。** 本服务只对应 Casdoor 的
   `pandabase` 组织下的 `mybmo-mcp` 应用；`built-in` 只服务 `dbx-mcp` / `casdoor-mcp`。
   两道闸：`MYBMO_OIDC_AUDIENCE` 只放行本服务的 client_id，`MYBMO_OIDC_ORGS`
   再校验令牌的 `owner` 必须是 `pandabase`。**授权表里没有 org / app 字段。**
8. **账户私有，owner 隐式管理员。** `tenancy.json` 的 `accounts[].owner` 是绑定时
   的 `sub`；owner 自动拥有 admin，别人要用必须显式 `grants`（只能 read/write）。
   没登记在 `accounts` 里的 alias 一律不可见。

## 二、环境变量（值由 infra 提供，变量名由本仓库决定）

| 变量 | 含义 | 缺失时 |
|---|---|---|
| `MYBMO_AUTH_MODE` | `local`（stdio 单用户）/ `oidc`（校验 JWT） | 默认 `local` |
| `CASDOOR_ISSUER` | 令牌 `iss`，必须精确匹配 | oidc 模式拒绝启动 |
| `MYBMO_OIDC_AUDIENCE` | 允许的 client_id 白名单。**生产只有 `709a8e16656a223a1ab7`（mybmo-mcp）** | oidc 模式拒绝启动 |
| `MYBMO_OIDC_ORGS` | 允许的 Casdoor 组织白名单（生产为 `pandabase`） | 留空则不校验组织 |
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
/data/tenancy.json     授权表 v2（accounts 归属 + grants 分享；尚未落库）
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

# OAuth 客户端：**只允许 mybmo-mcp 这一个应用**
mybmo-mcp / 709a8e16656a223a1ab7   本服务唯一入口（TTL 168h）
```

**用户自助路径**：用户在 `https://auth.pandabase.top/signup/mybmo-mcp` 自己注册 →
用 `mybmo-mcp` 应用登录 → 调 `bind_account` 拿百度授权链接 → 自己在浏览器授权 →
回调落库并把 owner 记成自己。全程不需要运维介入。

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
