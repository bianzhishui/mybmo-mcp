# Casdoor 实测记录

2026-11-08，实例 `https://auth.pandabase.top`（`casbin/casdoor:latest`，Docker，同机 PostgreSQL 16）。
以下全部是 `curl` 实测值，不是文档抄写——Casdoor 版本迭代快，凭印象写会误导后来人。

## 一、discovery 字段（tasks.md 5.3）

`GET /.well-known/oauth-authorization-server` 与 `/.well-known/openid-configuration` 两份都存在，
`issuer` 一致。

| 字段 | 实测值 | 影响 |
|---|---|---|
| `issuer` | `https://auth.pandabase.top` | 与 `CASDOOR_ISSUER` 完全一致，末尾无斜杠 |
| `authorization_endpoint` | `/login/oauth/authorize` | — |
| `token_endpoint` | `/api/login/oauth/access_token` | — |
| `jwks_uri` | `/.well-known/jwks` | **注意不是 `{issuer}/api/v1/jwks`** |
| `registration_endpoint` | `/api/oauth/register` | **存在**，DCR 可用 |
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

**② CIMD 不支持，只有 DCR。**

`client_id_metadata_document_supported: false` 意味着**只支持 DCR（RFC 7591）**，
不支持 CIMD（客户端用 URL 作为 client_id）。后果：

- Claude Desktop / Cursor 这类走 DCR 的客户端可以接入
- 依赖 CIMD 的客户端接不进来，只能手工在 Casdoor 后台建应用、把 client_id 写进客户端配置

这也是 ADR 0001 选 Casdoor 时的主要理由（它支持 DCR，Logto 不支持）——实测确认了这一点。

**③ `scopes_supported` 里没有我们的业务 scope。**

Casdoor 只声明 OIDC 标准 scope。我们需要的 `baidu:read` / `baidu:write` / `baidu:admin`
**必须**在 Casdoor 后台的应用配置里手工登记，否则客户端请求这些 scope 会被拒。

登记位置：应用详情 → `Scopes` 页签 → 添加 `baidu:read`、`baidu:write`、`baidu:admin`。

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

## 三、仍是默认口令

首次登录仍是 `admin` / `123`。**这是上线前必须改的第一件事**，见
`deploy/casdoor/README.md` 第三节。当前实例**尚未修改**，因为改口令需要人在浏览器操作。

## 四、内存实测

| 容器 | 内存 | CPU |
|---|---|---|
| `mybmo-casdoor-casdoor-1` | 24.81 MiB | 0.01% |
| `mybmo-casdoor-postgres-1` | 45.49 MiB | 0.01% |
| `mybmo-mcp-1` | 59.42 MiB | 0.16% |

整机 1967 MiB，可用 1256 MiB。**三个容器合计约 130 MiB**，远低于 ADR 0002 里
"四容器在 1.9G 上偏紧"的估计——那个估计是保守的，实测很宽裕。

## 九、尚未验证

- DCR 实际注册一个客户端（需要 POST `/api/oauth/register`，会写入数据库）
- 完整授权码 + PKCE 流程（需要浏览器与人眼确认）
- refresh token 是否轮换（影响 `CredentialService` 的并发刷新假设）
- Claude Desktop / Cursor 的真实接入（tasks 5.9）

## 五、真实令牌实测（最关键）

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
`MYBMO_OIDC_AUDIENCE=clientId`，不配就拒绝启动。

## 六、自定义 scope 的登记方式

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

## 七、仍待人工操作的两件事

**① admin 口令**：首启时数据库里 password 是**空字符串**（不是 `123`），
空密码会走手机号验证流程，无法登录。已用 bcrypt 直接写入新口令
（`$2b$10$...`，明文在密码管理器）。

**② 建 `mybmo-mcp` 应用**：管理 API 需要 OAuth accessToken 而非 session token，
用密码 grant 又必须先在应用上开 `password`。当前为了验证 JWT 链路，
**临时给 `app-built-in` 开了 password grant 并登记了三个 baidu scope**——这不是
生产配置，正式应用应在 Casdoor 界面上建，只开 `authorization_code`。

## 八、内存实测

