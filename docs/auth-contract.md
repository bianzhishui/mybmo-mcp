# 鉴权契约

本文件是 `mybmo-mcp` 与 `pandabase-infra` 之间的**代码级契约**。基础设施（Casdoor、PostgreSQL、
Caddy）的部署与运维在 infra 仓库，这里只记录**本仓库代码依赖的那些事实**。
改动本文件里的任何一条，都要同步通知 infra。

> ## ⚠️ 过渡期端口 `:8443`（2026-11-14 起）
>
> `pandabase.top` 的 ICP 备案正在审核；审核期间公网 **443 被备案拦截（RST）、80 被 302 到拦截页**，
> 所以 infra 把对外入口临时挪到 **`:8443`**（见 **pandabase-infra ADR 0005**）。
> 本文件当前所有 URL 均带 `:8443`，**是过渡值、不是新契约**——`issuer` / `resource` / 各端点语义不变，
> 只是端口变了。备案通过后 infra 会切回 443 无端口形态并通知，届时本文件需整体回退端口。
>
> 例外：**百度回调地址 `…/oauth/baidu/callback` 全程不带端口**——它必须与百度开放平台后台登记值
> 同步修改，而备案通过后原值就是对的，infra 决定全程不动。

## 一、认证全流程

一句话：**Casdoor 只回答"你是谁"，本服务只回答"你能用哪个百度账户"。**

### 角色

```
客户端(Cursor / DSH+mcp-remote) ──①登录──▶ Casdoor(auth.pandabase.top:8443)
                                ◀─②令牌──
                                ──③Bearer JWT──▶ mybmo-mcp（资源服务器）
                                                     │ ④取百度凭据
                                                     ▼
                                                 api.baidu.com
```

三条底线：**百度令牌不出服务端**；**本服务只验令牌、不签令牌**；**身份(sub) 与授权(账户) 分离**。

### 阶段 1：客户端登记（离线，一次性）

| 项 | 当前值 |
|---|---|
| 应用 | `mybmo-mcp` / client_id `709a8e16656a223a1ab7`（唯一） |
| 组织 | `pandabase`（应用只能登录同组织用户） |
| grant | `authorization_code` + `refresh_token` |
| scopes | `openid / profile / email / read / offline_access / baidu:read / baidu:write / baidu:admin` |
| 客户端类型 | **公共客户端**：只带 client_id + PKCE，无需 client_secret（见第二节不变量 8） |
| DCR | 关闭，客户端必须预置 client_id |

`client_id` 是给 **Casdoor** 认客户端用的；它出现在授权 URL 与 token 请求里，**不是给本服务看的**。

### 阶段 2：授权码 + PKCE（用户自己登录）

```
① 发现   GET /.well-known/oauth-protected-resource          ← 本服务
         ← {"resource":"https://baiduads.pandabase.top:8443/",
            "authorization_servers":["https://auth.pandabase.top:8443/"],
            "scopes_supported":["baidu:read"]}

② 元数据 GET https://auth.pandabase.top:8443/.well-known/oauth-authorization-server
         ← authorization_endpoint / token_endpoint / jwks_uri / registration_endpoint

③ 授权   GET {authorization_endpoint}?client_id=709a8e…&response_type=code
           &redirect_uri=http://localhost:<随机端口>/oauth/callback
           &scope=baidu:read offline_access
           &code_challenge=…&code_challenge_method=S256&state=…
         ← 用户在浏览器里用 pandabase 账号登录并同意
         ← 302 回 localhost:<端口>/oauth/callback?code=…&state=…

④ 换令牌 POST {token_endpoint}
           grant_type=authorization_code&code=…&code_verifier=…
           &client_id=709a8e…&redirect_uri=…
         ← JWT：sub=<用户UUID> / owner=pandabase / aud=["709a8e…"] /
                scope="baidu:read offline_access" / exp=+168h

⑤ 缓存   客户端本地存令牌，到期用 refresh_token 续期（Casdoor 默认轮换）
```

**前三步的请求落在哪里**：① ② 分别打本服务和 Casdoor 的元数据；③ ④ 才是浏览器与
token 端点；⑤ 之后每次工具调用才回到 `/mcp`。

### 阶段 3：每次调用的五道闸

```
POST /mcp   Authorization: Bearer <JWT>
 ① 签名     Casdoor JWKS（按 kid 取，缓存 10 分钟）        不通→401
 ② iss      精确等于 CASDOOR_ISSUER                      不通→401
 ③ aud      命中 MYBMO_OIDC_AUDIENCE（只放 709a8e…）      不通→401
 ④ exp/nbf  时钟偏移容忍 ≤60s                            不通→401
 ⑤ owner    命中 MYBMO_OIDC_ORGS（pandabase）             不通→401
 scope 检查：工具要求的 scope（如 baidu:read）             不足→403
```

未认证请求返回 `401 + WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"`，
把客户端指向 Casdoor。**③+⑤ 的语义是"必须是 pandabase 组织下 mybmo-mcp 应用签出的令牌"。**

### 阶段 3.5：`client_id` → 租户

```text
验签通过后取 client_id（azp 优先，退回 aud 的唯一值）
  → tenants WHERE client_id = ?   命中即用
  → 未命中：自动开户（id=t_<12hex>，name=租户-xxxx），并发首请求只建一条
  → 之后所有存储查询都带这个 tenant_id
```

- **一个 client_id 对应一个租户**（`tenants.client_id` 唯一索引）
- 准入闸门是**组织白名单**（`MYBMO_OIDC_ORGS`）；`MYBMO_OIDC_AUDIENCE` 是可选的收紧项
- 详见 [ADR 0008](decisions/0008-tenant-per-client.md)

### 阶段 4：业务判权（授权表；`MYBMO_TENANCY_BACKEND=file` 时是 `data/tenancy.json`）

见第五节数据卷布局与 `docs/architecture.md`。要点：账户**私有**，owner 隐式 admin，
分享只能 read/write，未登记的 alias 一律不可见，判定发生在**取凭据之前**。

### 阶段 5：取百度凭据 → 调百度

```
判权通过 → 取该账户加密凭据 → access 过期则加锁刷新（先落库再返回）
        → {header:{userName, accessToken, action}, body:{…}}
        → POST api.baidu.com/json/sms/service/<Service>/<Method>
```

`userName` 必须是**百度账户中文名**（填错报 81020）。账户改名后由刷新流程自动校正
（`getUserInfo` 的 `masterName`），不需要人工干预。

### 阶段 6：账户生命周期的三个离线动作

这三个动作**只在服务器上执行**，MCP 工具层不参与——应用密钥不能经过对话、审计或模型上下文。

| 动作 | 命令 | 契约要点 |
|---|---|---|
| 登记应用密钥 | `mybmo-mcp app secret set <appId> [--scope=…] [--note=…]` | `secretKey` 隐藏输入，写入 `app_secrets`（密文，AAD 绑 `appId`）。`app secret list` 只列 `appId`，**绝不回显密钥** |
| 生成授权链接 | `mybmo-mcp auth bind --alias <别名> --app-id <appId> [--subject <sub>] [--tenant <id>]` | 先按 `appId` 取密钥，**预写一条 `status=pending` 的占位凭据**，再签发一次性 `state`（15 分钟 TTL）。`--tenant` 缺省取 `MYBMO_TENANT_ID` |
| 解绑 | `mybmo-mcp auth unbind <别名>` | 删凭据 + 注销归属与全部分享。**换应用必须先解绑**，否则会撞唯一约束 |

MCP 侧的对应工具是 `bind_account(alias, app_id)`：只传 `appId`；只登记了一个应用时可省略，
登记了多个则必须显式指定（避免"猜一个应用"）。

约束（ADR 0006 D2/D6）：

- **一个百度账户在租户内只能绑定一个应用**，一个应用也只服务一个账户
- 冲突发生在**回调阶段**（授权前拿不到 `baidu_user_id`）：换到令牌后查唯一性，
  冲突则**丢弃令牌、不落库**，并提示 `auth unbind <已有别名>`
- `pending` 占位凭据不出现在 `list_accounts` 里；与 state 同为 900 秒 TTL

## 二、八个不能违背的不变量

1. **`aud` 默认为 client_id，但带 `resource` 参数时会变成资源地址。** Casdoor 收到
   RFC 8707 的 `resource` 就把它填进 `aud`（实测：带 `resource=https://baiduads.pandabase.top:8443/`
   时 `aud=["https://baiduads.pandabase.top:8443/"]`，不带时 `aud=["709a8e…"]`）。ADR 0008 之后
   校验规则是「**`aud` 必须包含该令牌自己的 client_id**」，所以**客户端仍然必须加
   `--disable-resource-parameter`**，否则令牌必被拒、并触发"授权→401→再授权"的死循环。
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
   见第三节的两条变量 `MYBMO_OIDC_AUDIENCE` / `MYBMO_OIDC_ORGS`。
7. **组织边界由鉴权层保证，授权表不重复表达。** 本服务只对应 Casdoor 的
   `pandabase` 组织下的 `mybmo-mcp` 应用；`built-in` 只服务 `dbx-mcp` / `casdoor-mcp`。
   两道闸：`MYBMO_OIDC_AUDIENCE` 只放行本服务的 client_id，`MYBMO_OIDC_ORGS`
   再校验令牌的 `owner` 必须是 `pandabase`。**授权表里没有 org / app 字段。**
8. **账户私有，owner 隐式管理员。** `tenancy.json` 的 `accounts[].owner` 是绑定时
   的 `sub`；owner 自动拥有 admin，别人要用必须显式 `grants`（只能 read/write）。
   没登记在 `accounts` 里的 alias 一律不可见。

## 三、环境变量（值由 infra 提供，变量名由本仓库决定）

| 变量 | 含义 | 缺失时 |
|---|---|---|
| `CASDOOR_ISSUER` | 令牌 `iss`，必须精确匹配 | oidc 模式拒绝启动 |
| `MYBMO_OIDC_AUDIENCE` | 允许的 client_id 白名单。**生产只有 `709a8e16656a223a1ab7`（mybmo-mcp）** | oidc 模式拒绝启动 |
| `MYBMO_MCP_READONLY` | 是否额外挂只读入口（ADR 0011） | 不设 = 关闭，装配与行为不变 |
| `MYBMO_MCP_READONLY_PATH` | 只读入口路径，默认 `/mcp/readonly` | 配成 `/mcp` 或其上级 → 拒绝启动 |
| `MYBMO_TEST_DATABASE_URL` | 仅测试用：设了才会跑 PG 仓储层集成测试 |
| `MYBMO_TENANCY_BACKEND` | `file`（默认）/ `pg`；`pg` 时授权表读 PostgreSQL |
| `MYBMO_DATABASE_URL` | `pg` 后端必填，`postgresql://...` |
| `MYBMO_OIDC_ORGS` | 允许的 Casdoor 组织白名单（生产为 `pandabase`） | 留空则不校验组织 |
| `MYBMO_JWKS_URL` | JWKS 地址 | 回落到 `{issuer}/.well-known/jwks`（不推荐） |
| `MYBMO_RESOURCE_URL` | 元数据里的 `resource` 字段 | oidc 模式拒绝启动 |
| `MYBMO_BAIDU_CALLBACK_URL` | 百度 OAuth 授权回调（`auth bind` 用） | 只影响绑定；缺失时报错，不影响检索与执行 |
| `MYBMO_BAIDU_SUBSCRIBE_SECRET` | 百度订阅推送的共享密钥 | `GET/POST /oauth/baidu/subscribe` 返回 503（失败关闭） |
| `MYBMO_DATA_DIR` | 凭据、审计、锁、`tenancy.json` 的根 | 默认 `./data` |
| `MYBMO_MASTER_KEY` | base64 的 32 字节 AES-GCM 主密钥 | 能否启动不报错，但调不动百度 |
| `MYBMO_DATABASE_URL` | 平台 PG 的 DSN（当前未启用） | 忽略 |

## 四、端点契约

| 端点 | 约定 |
|---|---|
| `GET /healthz` | 不需要鉴权；**即使 `credentials=unconfigured` 也返回 200**，用字段表达降级，不靠状态码。编排与监控依赖这一点 |
| `POST /mcp` | 需要 Bearer 令牌；未认证返回 401 且带 `WWW-Authenticate` 与 `resource_metadata` |
| `POST /mcp/readonly` | **可选**（`MYBMO_MCP_READONLY=1` 才存在，否则 404）。只读入口：7 个工具，`execute_capability` 对 `mutating != "false"` 一律拒绝并回 `denied` 审计。鉴权、`aud`、组织、授权表与 `/mcp` **完全共用**，OAuth 元数据也是同一份（`MYBMO_RESOURCE_URL` 是主机级，无路径）——所以客户端**不需要**新的授权流程。注意 `mcp-remote` 的缓存键含 URL：`/mcp` 与 `/mcp/readonly` 各需授权一次（全系统共 2 次，之后同一 OS 用户下全员复用） |
| `GET /oauth/baidu/callback` | **百度授权回调，不需要本服务令牌**——跳转过来的是浏览器，带不了 Bearer。安全性靠一次性 state + 15 分钟 TTL + "授权码只能换一次"。反代必须把这个路径也转到容器 |
| `GET/POST /oauth/baidu/subscribe` | **百度事件推送接收端，不需要本服务令牌**，靠共享密钥（`MYBMO_BAIDU_SUBSCRIBE_SECRET`）把关：未配 → 503，验不过 → 401 且不落盘。命中后**只打印并追加到 `data/subscribe/YYYY-MM-DD.jsonl`，不解析成任何业务动作**（ADR 0003） |
| `/.well-known/oauth-protected-resource` | 由 MCP SDK 发布，不需要自己实现 |

反代（Caddy）**不做鉴权**：它注入的身份头可被直连回环伪造，令牌校验必须在本进程内完成。

## 五、数据卷布局（已取消）

本服务**不再需要本地磁盘**：授权表、凭据、应用密钥、绑定 state、订阅样本、审计
全部在 PostgreSQL 里（ADR 0007 / 0009）。容器不挂载任何卷，`MYBMO_DATA_DIR`
配置项已删除。

历史上 `/data` 下放过的东西与去向：

| 旧路径 | 现在 |
|---|---|
| `/data/tenancy.json` | `tenants` / `users` / `baidu_accounts` / `account_grants` |
| `/data/accounts/*.json` | `credentials` |
| `/data/apps/*.json` | `app_secrets` |
| `/data/binding_state/*.json` | `binding_states` |
| `/data/audit/*.jsonl` | `audit_log` |
| `/data/subscribe/*.jsonl` | `subscribe_events` |
| `/data/locks/*.lock` | `SELECT … FOR UPDATE` 行锁 |

## 六、当前实例的取值（2026-11-14 · 备案过渡期）

```text
issuer   https://auth.pandabase.top:8443
jwks     https://auth.pandabase.top:8443/.well-known/jwks
resource https://baiduads.pandabase.top:8443
callback https://baiduads.pandabase.top/oauth/baidu/callback

# OAuth 客户端：**只允许 mybmo-mcp 这一个应用**
mybmo-mcp / 709a8e16656a223a1ab7   本服务唯一入口（TTL 168h）
```

> ⚠️ 相对 2026-11-11 的取值，`issuer` / `jwks` / `resource` 三个值因备案拦截**临时加了 `:8443`**；
> `callback` 保持不带端口（理由见文首）。
> `issuer` 本是"写进每个 JWT、改一次所有客户端都要重新配置"的不可变字段——**备案导致的加端口与
> 之后的回退端口，是它唯一被批准的两次变更**；这两次之后再动，就不是改配置，而是全体客户端重新接入。

**客户端接入（实测：不需要 client_secret）**：Casdoor 的 AS 元数据声明
`token_endpoint_auth_methods_supported` 含 `"none"`，实测也确实如此——mcp-remote 只带
`client_id` + PKCE 就能换到令牌（`scope=baidu:read offline_access`、`aud=[709a8e…]`、
`owner=pandabase`）。所以客户端配置里**只写 client_id**：

```yaml
args:
  - "-y"
  - "mcp-remote@latest"
  - "https://baiduads.pandabase.top:8443/mcp"
  - "--transport"
  - "http-only"
  - "--disable-resource-parameter"     # 否则 Casdoor 把 aud 填成资源地址 → 401 死循环
  - "--static-oauth-client-info"
  - '{"client_id":"709a8e16656a223a1ab7"}'
```

三点必须知道：

1. **`--disable-resource-parameter` 不是可选项**：带 `resource` 时 `aud` 会变成资源地址，
   进不了本服务的 client_id 白名单。
2. **不带 secret 意味着应用是公共客户端**：安全边界变成 `client_id + PKCE + aud 白名单
   + owner 白名单`。也就是说"单应用闸门"保证的是"令牌必须由本应用签出"，
   **不是"只有某个特定软件能连"**——任何知道 client_id 的人都能发起登录，
   但仍需自己的 pandabase 账号，且权限由 tenancy 按 `sub` 分配。
3. **未验证**：Casdoor 是否**根本不校验** client_secret（用错 secret 也能换令牌）。
   如果是，那配置里写 secret 毫无意义；要用一次真实授权码才能确认。

**用户自助路径**：用户在 `https://auth.pandabase.top:8443/signup/mybmo-mcp` 自己注册 →
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
