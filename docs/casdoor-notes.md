# Casdoor 实测记录

2026-11-08，实例 `https://auth.pandabase.top`（`casbin/casdoor:latest`，Docker，同机 PostgreSQL 16）。
以下全部是 `curl` / `psql` 实测值，不是文档抄写——Casdoor 版本迭代快，凭印象写会误导后来人。

## 一、discovery 字段（tasks 5.3）

`GET /.well-known/oauth-authorization-server` 与 `/.well-known/openid-configuration` 两份都存在，
`issuer` 一致。

| 字段 | 实测值 | 影响 |
|---|---|---|
| `issuer` | `https://auth.pandabase.top` | 与 `CASDOOR_ISSUER` 完全一致，末尾无斜杠 |
| `authorization_endpoint` | `/login/oauth/authorize` | — |
| `token_endpoint` | `/api/login/oauth/access_token` | — |
| `jwks_uri` | `/.well-known/jwks` | **注意不是 `{issuer}/api/v1/jwks`** |
| `registration_endpoint` | `/api/oauth/register` | 存在，但组织级 DCR 已关（见第五节） |
| `client_id_metadata_document_supported` | **`false`** | **CIMD 不支持** |
| `code_challenge_methods_supported` | `["S256"]` | PKCE 可用，且只支持 S256 |
| `response_types_supported` | `code` 等 7 种 | — |
| `scopes_supported` | `openid` / `email` / `profile` / `address` / `phone` / `offline_access` | **没有 `baidu:read`** |

### 三条必须改代码/配置的事实

**① `jwks_uri` 与我们推导的不一样。**

`JwtVerifier` 默认按 `{issuer}/api/v1/jwks` 推导，而这个实例给的是
`{issuer}/.well-known/jwks`。两者都返回同一把密钥（实测 `kid=cert-built-in`、`kty=RSA`、
`alg=RS256`、`use=sig`），但**不要依赖推导**——部署时显式设 `MYBMO_JWKS_URL`：

```bash
MYBMO_JWKS_URL=https://auth.pandabase.top/.well-known/jwks
```

推导值能用是巧合，不是契约。换个 Casdoor 版本或改部署方式就可能失效。

**② CIMD 不支持。**

`client_id_metadata_document_supported: false` 意味着客户端不能用 URL 当 client_id，
只能靠 DCR（RFC 7591）或管理员手工建应用。而 DCR 在本实例上被证伪（第五节），
所以实际只剩**手工建应用**这一条路。

**③ `scopes_supported` 里没有我们的业务 scope。**

Casdoor 只声明 OIDC 标准 scope。我们需要的 `baidu:read` / `baidu:write` / `baidu:admin`
**必须**在 Casdoor 后台的应用配置里手工登记，否则客户端请求这些 scope 会被拒。

如果忘了这步，表现是：登录成功、拿到令牌，但令牌里 `scope` 只有 `openid profile`，
于是 `required_scopes=["baidu:read"]` 判定失败，每个请求 403。

## 二、JWKS 内容

```
key 数量: 1
  kid=cert-built-in  kty=RSA  alg=RS256  use=sig
```

只有一把内置密钥，对应 Casdoor 该应用的 `Cert`。轮换方式：在应用里换 Cert，
`kid` 会变，我们的 `JwksCache` 按 `kid` 查找所以能跟上——但缓存 TTL 是 10 分钟，
换密钥后最多有 10 分钟窗口期会验签失败。需要立刻生效就重启 mcp 进程。

**DCR 建出来的应用 `cert` 字段是空字符串，但令牌的 `kid` 仍是 `cert-built-in`**
（实测），说明 Casdoor 回退到内置证书签名。JWKS 因此对所有应用都够用。

## 三、真实令牌实测（最关键）

用 password grant 拿到的真实 JWT，claims 如下：

```json
{
  "iss": "https://auth.pandabase.top",
  "aud": ["60fda1d9bedd4615eb90"],          ← **client_id，不是服务地址**
  "sub": "ccc3493b-abc5-4f8d-a73a-a2d67e711872",
  "azp": "60fda1d9bedd4615eb90",
  "scope": "openid profile baidu:read baidu:write",
  "exp": 1794715287
}
```

同一个令牌，两种 audience 配置：

| 校验器 audience | 结果 |
|---|---|
| `https://baiduads.pandabase.top`（架构文档原假设） | 被拒「audience 不是本服务」 |
| `60fda1d9bedd4615eb90`（真实 client_id） | **通过**，`sub` 与 scopes 均正确 |

签名与 `iss` 两处验证均通过 → JWKS 路径 `/.well-known/jwks` 正确。
唯一不符的是 `aud`。

**这推翻了 `docs/architecture.md` 第二层的假设**，已写进
[ADR 0006](decisions/0006-casdoor-audience.md)。结论：`aud` 校验对象是
「允许调用本服务的 OAuth 客户端」，不是资源地址；因此新增必配项
`MYBMO_OIDC_AUDIENCE`（可逗号分隔多个 client_id），不配就拒绝启动。

## 四、自定义 scope 的登记方式

Casdoor 的 `scopes` 字段是**对象数组**，不是字符串数组。写成 `["baidu:read"]` 会报：

```
json: cannot unmarshal string into Go value of type object.ScopeItem
```

正确形状（可直接用于 `psql`）：

```sql
UPDATE application SET scopes = '[
  {"name":"baidu:read","displayName":"baidu:read","description":"只读百度营销接口",
   "icon":"","value":"","required":false,"isLogoVisible":false},
  {"name":"baidu:write","displayName":"baidu:write","description":"可写百度营销接口",
   "icon":"","value":"","required":false,"isLogoVisible":false},
  {"name":"baidu:admin","displayName":"baidu:admin","description":"管理账户与授权",
   "icon":"","value":"","required":false,"isLogoVisible":false}
]' WHERE name = 'app-built-in';
```

改完要 `docker restart` Casdoor，配置才生效。实测令牌里出现
`scope: "openid profile baidu:read baidu:write"`，说明登记成功。

## 五、DCR 实测结论：不开（tasks 5.4）

任务原计划是「开组织级 DCR + 回调白名单 + 注册频率限制」。逐项实测后，**三样里有两样不存在**。

### 5.1 策略只有两个取值

`organization.dcr_policy` 是 `varchar(100)`，但前端 `OrganizationEditPage` 只给一个开关：

```js
onCheckedChange: e => n("dcrPolicy", e ? "open" : "disabled")
```

即 `open` / `disabled`，没有白名单模式、没有限流参数。

### 5.2 开启后任意回调地址都收

```bash
POST /api/oauth/register  {"redirect_uris":["http://127.0.0.1:33418/callback"], ...}
→ 201 Created，新建应用 dcr_wh7gkj

POST /api/oauth/register  {"redirect_uris":["https://evil.example.com/cb"], ...}
→ 201 Created，新建应用 dcr_nbpi0q      ← 敌意域名照样通过

POST /api/oauth/register  {"client_name":"no-redirect"}
→ 400 invalid_redirect_uri "redirect_uris is required and must contain at least one URI"
```

唯一的校验是「至少有一个 URI」。没有域名白名单、没有回环限制、没有频率限制，
每次请求都在 `application` 表里加一行。

### 5.3 DCR 拿到的令牌调不动本服务

```
DCR 客户端 06fbc3... 的令牌 → aud = ["06fbc30987245dbe95e9"]（它自己的 client_id）
线上校验器（白名单 60fda1...） → 被拒：令牌的 audience 不在允许的客户端列表里
```

Casdoor 也不给改写的机会：传 `resource=`（RFC 8707）和 `audience=`
两种参数都被**默默忽略**，`aud` 恒等于 client_id。

### 结论

开了 DCR 只会白送一个可被用于钓鱼的注册端点（code 落到攻击者域名），
换不来自助接入。因此：

- `dcr_policy` 保持 `disabled`，注册端点实测返回
  `400 invalid_client_metadata "dynamic client registration is disabled for this organization"`
- 客户端改为**逐个显式注册**，client_id 加进 `MYBMO_OIDC_AUDIENCE` 白名单
- 回调白名单由**应用级** `redirect_uris` 承担（一个应用一个回调）

完整推理与重新评估的触发条件见 [ADR 0007](decisions/0007-no-dcr.md)。
探针应用已删除，`application` 表只剩 `app-built-in` 一行。

### 5.4 部署后 A/B

白名单上线后，用那个 DCR 客户端签发的令牌在线上做了一次对照（同一个令牌，只改配置）：

| `MYBMO_OIDC_AUDIENCE` | 该令牌 POST `/mcp` |
|---|---|
| `60fda1d9bedd4615eb90` | **401** |
| `60fda1d9bedd4615eb90,06fbc30987245dbe95e9` | **200** |
| 改回单值 | **401** |

白名单内的正常令牌始终 200。说明「加一个 client_id 才放行一个客户端」在真实部署里成立。

## 六、端到端验证结果（2026-11-08 12:10）

线上 `https://baiduads.pandabase.top/mcp` 已切到 `MYBMO_AUTH_MODE=oidc`，实测：

| 场景 | 结果 |
|---|---|
| 无令牌 POST `/mcp` | **401**，`WWW-Authenticate: Bearer ... resource_metadata=".../.well-known/oauth-protected-resource"` |
| 伪造令牌 | **401** |
| 只有 `openid` 的令牌 | **403**（scope 不足） |
| 含 `baidu:read` 的令牌 | **200**（MCP 握手成功） |
| MCP 客户端列工具 | 5 个，与 local 模式一致 |
| `list_accounts` | `ok: true`，`identity.sub` = Casdoor 用户 UUID，`source: oidc`，`unrestricted: false` |

最后一条是本次最关键证据：**身份确实来自 JWT，不再是 `local_admin`**。
`unrestricted: false` 说明没有回退成本机全权身份，`grants: {}` 说明这个用户还没有账户授权
（`count: 0`，符合预期——还没绑定任何百度账户）。

## 七、内存实测

| 容器 | 内存 | CPU |
|---|---|---|
| `mybmo-casdoor-casdoor-1` | 24.81 MiB | 0.01% |
| `mybmo-casdoor-postgres-1` | 45.49 MiB | 0.01% |
| `mybmo-mcp-1` | 59.42 MiB | 0.16% |

整机 1967 MiB，可用 1256 MiB。**三个容器合计约 130 MiB**，远低于 ADR 0002 里
"四容器在 1.9G 上偏紧"的估计——那个估计是保守的，实测很宽裕。

## 八、上线前必须处理的两件事

**① `app-built-in` 还开着 `password` grant。**

为了让命令行能验证 JWT 链路，**临时**开了 `password`
（Casdoor 默认只允许 `authorization_code` + `refresh_token`）。
意味着任何持有 Casdoor 账号密码的人都能不经浏览器直接换令牌。
当前只有 `admin` 一个用户，风险可控；**对外前必须关掉**：

```sql
UPDATE application
SET grant_types = '["authorization_code","refresh_token"]'
WHERE name = 'app-built-in';
```

关掉后命令行调试要临时再开，或走浏览器授权码流程。

**② 正式应用还没建。**

现在用的是 `app-built-in`（Casdoor 自带的示例应用，名字就叫 "Casdoor"）。
生产应该另建一个 `mybmo-mcp` 应用，只开 `authorization_code` + `refresh_token`，
把它的 clientId 填进 `MYBMO_OIDC_AUDIENCE`。

顺带记一个坑：**首启时数据库里 admin 的 password 是空字符串**（不是文档说的 `123`），
空密码会走手机号验证流程，无法登录。已用 bcrypt 直接写入新口令（明文在密码管理器）。

## 九、尚未验证

- 完整授权码 + PKCE 流程（需要浏览器，人与眼确认）
- refresh token 是否轮换（影响 `CredentialService` 的并发刷新假设）
- Claude Desktop / Cursor 的真实接入（tasks 5.9，依赖上面两条）
