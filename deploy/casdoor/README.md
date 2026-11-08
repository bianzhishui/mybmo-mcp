# Casdoor 首次部署与配置

这台机器上 mcp 已经在跑（`MYBMO_AUTH_MODE=local`，basic auth 挡着）。加 Casdoor 是
**第二步**，做完才能把 Claude Desktop 这类只认 OAuth 的客户端接进来。

前提：`auth.pandabase.top` 的 A 记录已解析到本机（`dig +short auth.pandabase.top` 有值）。

## 一、起容器

```bash
cd /opt/mybmo-mcp
cp deploy/casdoor/.env.example deploy/casdoor/.env
openssl rand -base64 24   # 填进 POSTGRES_PASSWORD
vim deploy/casdoor/.env

docker compose -f deploy/casdoor/docker-compose.yml up -d
docker compose -f deploy/casdoor/docker-compose.yml ps
```

Casdoor 首次启动会初始化表结构，约 30–60 秒。等 `healthy` 再看下一步。

## 二、让 Caddy 接管 auth 子域

追加到 `/etc/caddy/Caddyfile`：

```caddyfile
auth.pandabase.top {
	reverse_proxy 127.0.0.1:8001
}
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy
curl -fsS https://auth.pandabase.top/ | head -3   # 应返回 Casdoor 登录页 HTML
```

**这一步证书必须成功。** Casdoor 的 `origin` 是 https，如果 TLS 不通，
它签出的 JWT 里 `iss` 依然对，但客户端拿不到登录页。

## 三、改默认口令、建管理员

Casdoor 初始管理员是 `admin` / `123`。**先改掉再往下走**：

1. 打开 `https://auth.pandabase.top`
2. 用 `admin` / `123` 登录
3. 右上角头像 → 修改密码 → 设一个强口令
4. 记进密码管理器

## 四、建应用（关键，五个字段都要对）

`Applications` → `Add`，然后按下面填：

| 字段 | 值 | 为什么必须是这个 |
|---|---|---|
| Name | `mybmo-mcp` | 会作为 `client_id` 出现在令牌里 |
| Display name | `mybmo 营销网关` | 登录页显示 |
| **Redirect URLs** | 先留空，见第五节 | 留空 = 拒绝一切回调，正好用来先验证 discovery |
| Cert | 用默认生成的证书 | 它的私钥用来签 JWT，JWKS 也来自它 |
| **Signin methods** | 打开 `Password` | 至少留一种，否则没法人机登录 |

再进 `Tokens` 页签确认：

- `Access token expiry` 建议 **1 小时**（默认可能更长）
- `Refresh token expiry` 建议 **30 天**
- **不要开** `Enable PKCE` 的反向选项——MCP 客户端自己会带 PKCE 参数

## 五、验证 discovery（tasks 5.3）

这是最容易被跳过、又决定成败的一步。**先确认 Casdoor 到底暴露了什么**：

```bash
curl -s https://auth.pandabase.top/.well-known/oauth-authorization-server | python3 -m json.tool
```

要看的三个字段：

| 字段 | 期望 | 没有时的后果 |
|---|---|---|
| `registration_endpoint` | 存在 | 客户端无法自助注册，只能手工配 client_id |
| `client_id_metadata_document_supported` | `true`（若支持 CIMD） | 老客户端接不进来 |
| `authorization_endpoint` / `token_endpoint` | 存在 | 登录流程走不通 |

把实际返回值记进 `docs/casdoor-notes.md`——**不要凭印象写**，Casdoor 版本迭代快，
不同版本这些字段差异很大。

## 六、开 DCR（tasks 5.4）

`Applications` 页有个组织级设置。开启后：

- 回调 URI 白名单：先只放 `http://127.0.0.1:*` 与 `https://claude.ai/*` 这类已知模式
- 注册频率限制：按 IP，防止被刷成垃圾应用

**不做这步的后果**是任何人可以注册一个 client 然后把你这里当跳板。
虽然最终还要过 `tenancy.json` 的授权，但没必要让陌生人先拿到有效令牌。

## 七、切 mcp 到 oidc 模式

```bash
cd /opt/mybmo-mcp
# deploy/.env 改三处
#   MYBMO_AUTH_MODE=oidc
#   CASDOOR_ISSUER=https://auth.pandabase.top
#   MYBMO_RESOURCE_URL=https://baiduads.pandabase.top
docker compose -f deploy/docker-compose.yml up -d --force-recreate
```

**切换后必须建 `data/tenancy.json`**，否则所有调用报"缺少授权表"——这是有意的：

```bash
docker exec mybmo-mcp-1 cat /data/tenancy.json 2>/dev/null || echo "还没有，需要创建"
```

格式见仓库根 `tenancy.example.json`。`sub` 填 Casdoor 里的用户名（不是邮箱）。

## 八、回滚

Casdoor 出问题时把 `MYBMO_AUTH_MODE` 改回 `local` 并重启 mcp，basic auth 立刻恢复生效——
这是当初选择"两套编排分离"的原因之一：

```bash
docker compose -f deploy/docker-compose.yml down
# 改回 MYBMO_AUTH_MODE=local
docker compose -f deploy/docker-compose.yml up -d
```

## 九、内存

Casdoor（Go）+ PostgreSQL 在同一台 1967 MiB 的机器上跑，实测前不要假设够用。
起容器后立刻看：

```bash
docker stats --no-stream
free -m
```

若可用内存低于 400 MiB，考虑把 PostgreSQL 换成外部实例，或只保住 mcp 不动。
