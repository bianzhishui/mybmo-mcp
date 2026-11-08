# 运维手册

目标形态：一台公网服务器，Caddy 负责 TLS 与访问控制，MCP 服务只在容器内网可达。
下面是**当前阶段**（Casdoor 未接入）的完整流程。

```text
公网 ──:443──▶ 宿主 Caddy（systemd 托管）──127.0.0.1:8000──▶ mcp 容器
                    │
                    └ /healthz 不鉴权，其余路径要 basic auth
```

TLS 交给宿主上已有的 Caddy（`/etc/caddy/Caddyfile`），不额外跑容器：
少一个进程，也避免和机器上别的站点抢 80/443。若这台机器是专用的，改用
`--profile dedicated` 起容器版 Caddy（见第五节）。

## 一、前置条件

| 项 | 要求 | 检查方式 |
|---|---|---|
| DNS | `baiduads` **和** `auth` 两条 A 记录都指向本机公网 IP | `dig +short baiduads.pandabase.top` |
| 端口 | 安全组 / 防火墙放通 80、443（申请证书要用 80） | `nc -z 公网IP 443` |
| Docker | 20.10+，含 compose v2 | `docker compose version` |
| 内存 | mcp + Caddy 约 200 MB，1.9 GiB 机器足够 | `free -m` |

**通配符 `*.pandabase.top` 不等于有记录**——实测这台机器的权威 NS（DNSPod）上
只有 apex 记录，子域必须显式加。DNS 没生效就启动，Caddy 会反复重试签证书并失败，
先 `dig` 确认再往下走。

## 二、首次部署

```bash
git clone <repo> /opt/mybmo-mcp
cd /opt/mybmo-mcp

cp deploy/.env.example deploy/.env
# 至少要填三项：MCP_HOST、ACME_EMAIL、MCP_PASSWORD_HASH
# 主密钥用：python3 -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"
vim deploy/.env

docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml ps
```

生成口令哈希（不要在别处复用这个密码）：

```bash
caddy hash-password --plaintext '你的密码'     # 宿主已装 caddy 时
# 或
docker run --rm caddy:2-alpine caddy hash-password --plaintext '你的密码'
```

### 让 Caddy 接管这个域名

宿主机已有 Caddy 服务时**不要**再起 Caddy 容器，把
[`deploy/caddy-site.caddyfile`](../deploy/caddy-site.caddyfile) 里的站点块追加到
`/etc/caddy/Caddyfile`，把 `mybmo $2a$14$REPLACE...` 换成真实用户名与哈希：

```bash
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$(date +%F)
sudo tee -a /etc/caddy/Caddyfile < deploy/caddy-site.caddyfile   # 追加后再手工改哈希
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

原来的默认站点（`:80 { file_server }`）留着不影响；它只服务 IP 直连，域名走新块。

专用宿主机则改用容器版：

```bash
docker compose -f deploy/docker-compose.yml --profile dedicated up -d --build
```

验证：

```bash
curl -fsS https://baiduads.pandabase.top/healthz
# {"status":"ok","version":"0.1.0","auth_mode":"local","capabilities":389,
#  "described":50,"credentials":"unconfigured"}
```

`credentials=unconfigured` 说明 `MYBMO_MASTER_KEY` 没填对——此时服务能检索接口目录，
但调不动百度。这是刻意的：没有密钥不该让容器起不来，只该让它干不了活。

顺带确认鉴权真的生效（不带凭据应当被拒）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'Content-Type: application/json' -d '{}' https://baiduads.pandabase.top/mcp
# 期望 401
```

## 三、接客户端

MCP 端点是 `https://baiduads.pandabase.top/mcp`，走 Streamable HTTP。
因为现在是 basic auth 而不是 OAuth，客户端要能自定义请求头。

**已经实测通过的方式**（Python MCP SDK）：

```python
import base64, httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

auth = "Basic " + base64.b64encode(b"mybmo:密码").decode()
async with httpx2.AsyncClient(headers={"Authorization": auth}, timeout=20.0) as http:
    async with streamable_http_client("https://baiduads.pandabase.top/mcp", http_client=http) as s:
        async with ClientSession(s[0], s[1]) as session:
            await session.initialize()
            (await session.list_tools()).tools   # 5 个
```

命令行方式（需要 Node）：

```bash
npx -y mcp-remote@latest https://baiduads.pandabase.top/mcp \
  --header "Authorization: Basic $(printf 'mybmo:密码' | base64)"
```

直接支持 `headers` 的客户端（如 Cursor）可以把这一行写进配置。
**Claude Desktop 的远程连接要求 OAuth，接不了 basic auth**——它要等 Casdoor。

## 四、健康检查与监控

`/healthz` 不需要鉴权，也不返回任何账户或令牌信息：

| 字段 | 含义 | 告警建议 |
|---|---|---|
| `status` | 恒为 `ok`，进程能应答就是活着 | 连续 3 次超时 → 重启容器 |
| `credentials` | `configured` / `unconfigured` | 变 `unconfigured` → 主密钥丢了或没配 |
| `capabilities` | 能力条数，正常是 389 | 变 `null` 或变少 → 镜像打包有问题 |

Docker 自带的 HEALTHCHECK 已指向它，`docker ps` 里能看到 `healthy`。

## 五、日志

```bash
docker compose -f deploy/docker-compose.yml logs -f mcp     # 应用日志（stderr）
sudo journalctl -u caddy -f                                # 证书与访问日志（宿主 Caddy）
```

**审计日志不在 stdout**，在数据卷里，按天分文件：

```bash
docker compose -f deploy/docker-compose.yml exec mcp \
  sh -c 'ls -l /data/audit && tail -n 5 /data/audit/$(date +%F).jsonl'
```

每行一条 JSON，含 `sub` / `account` / `capability` / `state` / `payload_digest`，不含报文与令牌。

## 六、备份

**两样东西必须分开备份**：数据卷（加密凭据 + 审计）和 `deploy/.env`（主密钥）。

```bash
# 数据卷
docker run --rm -v mybmo_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/mybmo-data-$(date +%F).tgz -C /data .

# 配置与主密钥（含密钥，存到密码管理器或加密盘，别和备份放一起）
cp deploy/.env ~/mybmo-deploy.env.bak
```

**主密钥丢了 = 所有已绑定账户作废**，只能用旧备份恢复，或让每个账户重新走一次百度授权。
这一条没有补救办法，加密是 AES-GCM，没有后门。

恢复：

```bash
docker compose -f deploy/docker-compose.yml down
docker run --rm -v mybmo_data:/data -v "$PWD":/backup alpine \
  sh -c 'cd /data && tar xzf /backup/mybmo-data-YYYY-MM-DD.tgz'
docker compose -f deploy/docker-compose.yml up -d
curl -fsS https://baiduads.pandabase.top/healthz
```

## 七、升级与回滚

```bash
git pull
docker compose -f deploy/docker-compose.yml up -d --build
```

数据卷不动，升级不影响已绑定的账户。能力表（`capabilities.json`）是**构建期产物**，
镜像里带的是仓库里的版本；百度接口有变动时在开发机重新生成再提交，不在服务器上跑生成器。

回滚：

```bash
git log --oneline -5
git checkout <上一个提交或 tag>
docker compose -f deploy/docker-compose.yml up -d --build
```

## 八、什么时候加 Casdoor

现在不接，原因是它还不需要：`MYBMO_AUTH_MODE=local` 下没有"用户"这个概念，
basic auth 挡住外人就够了。等下面任意一条成立再上：

- 要给别人用，且不想共享一个口令
- 需要按账户授权（A 只能看 `axd`，B 能改 `bcd`）
- 需要审计到人（现在审计里的 `sub` 恒为 `local`）

接入时必须一次做对的事：**issuer 定死就是 https://auth.pandabase.top，之后不能改**，
它写进每个 JWT 的 `iss`（理由见 ADR 0001）。步骤：

1. DNS 加 `auth.pandabase.top` 的 A 记录
2. 解除 `docker-compose.yml` 里 casdoor / postgres 的注释
3. 解除 `Caddyfile` 里 auth 站点块的注释
4. `deploy/.env` 设 `CASDOOR_ISSUER`、`MYBMO_RESOURCE_URL`、`MYBMO_DATABASE_URL`
5. 起容器，在 Casdoor 后台建应用，回调地址填 MCP 客户端用的那个
6. 建用户与 `data/tenancy.json`（格式见 `tenancy.example.json`）
7. `MYBMO_AUTH_MODE=oidc` 重启，**此时缺授权表会直接报错**，这是有意的
8. 客户端从 basic auth 换成 OAuth

## 九、常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| Caddy 日志报 ACME 失败 | 子域没有 A 记录 / 80 端口未放通 | `dig +short baiduads.pandabase.top`，再查安全组 |
| `401 Unauthorized` | 没带 basic auth，或哈希写错 | 重新 `caddy hash-password` 并 reload |
| 容器 `unhealthy` | `/healthz` 打不通 | `logs mcp` 看是否启动即崩 |
| 502 Bad Gateway | Caddy 连不到 `127.0.0.1:8000` | `docker compose ps` 看 mcp 是否在跑、端口映射是否被占 |
| 工具调用挂住不返回 | 反向代理缓冲了 SSE | 本配置默认不缓冲；若改过 Caddyfile 检查 `flush_interval` |
| `credentials: unconfigured` | `MYBMO_MASTER_KEY` 缺失或格式错 | 32 字节 base64，重启生效 |
| 所有账户报 `reauth_required` | 换了主密钥 | 用原密钥，或重新绑定 |

## 十、现状（2026-11-08 上线记录）

| 项 | 实测值 |
|---|---|
| 域名 | `baiduads.pandabase.top` → 49.232.229.71（DNSPod，`auth` 子域也已解析） |
| TLS | Let's Encrypt，CN=baiduads.pandabase.top，有效期至 2027-02-06，自动续期 |
| 部署形态 | 共享宿主机：宿主 Caddy（systemd）+ mcp 容器（`127.0.0.1:8000`） |
| 镜像 | `mybmo-mcp:0.1.0`，258 MB |
| 内存占用 | 容器 56 MiB，宿主 Caddy 约 30 MiB，整机 1967 MiB 中可用 1342 MiB |
| 构建耗时 | 首次 13.5 分钟（已配腾讯云镜像加速；未配时 ghcr 单层就要 745 秒） |

已验证：`/healthz` 200、无凭据 `/mcp` 401、带凭据 400（握手拒绝空 body）、
MCP 客户端列出 5 个工具、`search_capabilities("报表")` 命中 3 条。

**踩过的坑**（都已在代码里修掉，留档避免复发）：

1. `MCP_PASSWORD_HASH` 含 `$`，写进 compose 的 `environment: ${VAR}` 会被当变量插值 →
   改为 `env_file` 或宿主 Caddyfile。共享宿主机形态下根本不该写进 `.env`。
2. `ghcr.io/astral-sh/uv` 基础镜像在境内拉取极慢 → Dockerfile 改用 `python:3.12-slim` + pip 装 uv。
3. `psycopg` / `pyjwt` 当前零引用却每次构建都要下载 → 移到 `optional-dependencies.p4`。
4. **通配符不等于有记录**：DNSPod 上 apex 有 A 记录，`baiduads` / `auth` 必须显式添加。

## 十一、还没验证的

- **Docker vs systemd**：本文按 Docker 写。若目标机不允许装 Docker，
  需另写 systemd unit，`MYBMO_DATA_DIR` 指向 `/var/lib/mybmo`，其余不变
- **备份恢复演练**：当前无已绑定账户，恢复验证无意义，等首个账户绑定后补
- **Claude Desktop 直连**：它的远程连接要 OAuth，在 Casdoor 接入前接不了 basic auth
- **Casdoor + PostgreSQL 的实机内存**：当前余量够，但没装过，不能只凭估算
