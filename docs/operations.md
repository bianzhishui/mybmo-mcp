# 运维手册

目标形态：一台公网服务器，Caddy 负责 TLS，MCP 服务只在容器内网可达。
下面是**线上现状**（2026-11-08：mcp + Casdoor + PostgreSQL 17.11，`oidc` 模式）的完整流程。

```text
公网 ──:443──▶ 宿主 Caddy（systemd 托管）──127.0.0.1:8000──▶ mcp 容器
                    │
                    └ /healthz 不鉴权；/mcp 走 OAuth Bearer，反代不做鉴权
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

MCP 端点是 `https://baiduads.pandabase.top/mcp`，走 Streamable HTTP，
认证是 **OAuth Bearer**（`MYBMO_AUTH_MODE=oidc`）。Caddy 上**没有** basic auth。

无令牌访问会拿到：

```http
HTTP/2 401
www-authenticate: Bearer error="invalid_token",
  resource_metadata="https://baiduads.pandabase.top/.well-known/oauth-protected-resource"
```

**已经实测通过的方式**（Python MCP SDK + Casdoor 真实令牌）：

```python
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

token = "..."  # Casdoor 签发的 access_token
async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=20.0) as http:
    async with streamable_http_client("https://baiduads.pandabase.top/mcp", http_client=http) as s:
        async with ClientSession(s[0], s[1]) as session:
            await session.initialize()
            (await session.list_tools()).tools  # 5 个
```

命令行调试取令牌（`app-built-in` 现在临时开着 password grant，见
[`casdoor-notes.md`](casdoor-notes.md) 第八节①，**对外前要关**）：

```bash
curl -s https://auth.pandabase.top/api/login/oauth/access_token \
  -d grant_type=password -d client_id=$CID -d client_secret=$CSECRET \
  -d username=$USER -d password=$PASS -d 'scope=openid baidu:read'
```

桌面客户端（Claude Desktop / Cursor）**不能贴 URL 直接用**：它们要自助注册，
而我们不放 DCR（[ADR 0007](decisions/0007-no-dcr.md)）。必须先按 8.1 在 Casdoor 建应用、
把回调地址填成该客户端的固定值、clientId 加进 `MYBMO_OIDC_AUDIENCE`。
这条浏览器授权码流程**尚未实跑**（tasks 5.9）。

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

**Casdoor 的库不在这两个卷里。** 账号、应用、注册的 scope、JWT 签名证书都在 PostgreSQL 的
卷 `mybmo-casdoor_pgdata` 上，要单独备份一次（整卷 tar，或 `pg_dump -Fc`，产物形状见 7.1）。
这个卷丢了，签名证书一起没——JWKS 换钥，**所有已签发令牌立即失效**，每个客户端都要重新注册。
它比 mcp 的数据卷更值得备份。

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

### 7.1 PostgreSQL 大版本升级（16 → 17）

> **已完成**：2026-11-08 13:32 线上迁到 **PG 17.11**，行数核对一致（50 表 / 1 应用 / 1 用户 /
> 1 组织），Casdoor 零错误日志，`/healthz` 200、`/mcp` 无令牌 401。本节保留作流程记录与回滚参考。

`deploy/casdoor/docker-compose.yml` 已按 [ADR 0002](decisions/0002-tech-stack.md) D3 钉在
`postgres:17-bookworm`（非 alpine，避开 musl 的中文 locale 缺陷）。线上实例是 P4 时起的
`postgres:16-alpine`，**两者的数据目录不通用**：直接 `up -d` 会因卷里的 `PG_VERSION` 是 16
而拒绝启动。数据不会丢，但 Casdoor 一直起不来，`/mcp` 全线 401。

Casdoor 数据量极小（2026-11-08 实测：`application` 1 行、用户 1 个），走
「`pg_dump -Fc` → 重建卷 → `pg_restore`」停机 1~3 分钟，不需要 `pg_upgrade` 那套就地升级。

#### 执行

```bash
ssh root@49.232.229.71
cd /opt/mybmo-mcp
git pull                                    # 先让 compose 与脚本落地
bash scripts/pg-upgrade-16-to-17.sh         # 要求输入 MIGRATE 确认；无人值守加 --yes
```

`git pull` 若被 GnuTLS 打断，改用 `git archive HEAD | ssh ... 'tar -x -C /opt/mybmo-mcp'`
整包覆盖（见「踩过的坑」之前那节的经验）。

脚本按顺序做 6 步，**任何一步的自检不过就停下**，不会带着坏备份往下走：

| 步 | 动作 | 自检 |
|---|---|---|
| 0 | 读卷名、`PG_VERSION`、磁盘余量 | 不是 16 就退出；已是 17 则查 `application` 行数后退出 |
| 1 | `pg_dump -Fc` 到 `/root/casdoor-pg-upgrade/<STAMP>/` | 文件非空 + `pg_restore -l` 里有 `TABLE DATA` |
| 2 | `compose stop` 后整卷 `tar czf` | 归档里有 `PG_VERSION` |
| 3 | `compose down` → `docker volume rm` → `up -d postgres` | `pg_isready` 90 秒内就绪；新卷 `PG_VERSION=17` |
| 4 | `pg_restore --no-owner --no-privileges` | 退出码与 stderr 落到 `restore.err` |
| 5 | 对比迁移前后行数 | `tables` / `application` / `user` / `organization` 四项必须一致 |
| 6 | `up -d` 起全套 + 拉 discovery | `/well-known/openid-configuration` 可达 |

第 3 步只删 **postgres** 的数据卷；`casdoor_data`（Casdoor 自己的文件卷）不动。

#### 为什么用 `pg_dump -Fc` 而不是 `pg_dumpall`

新集群由镜像按 `POSTGRES_USER` / `POSTGRES_DB` 已经建好 `casdoor` 角色与同名库，
`pg_dumpall` 产物里的 `CREATE ROLE` / `CREATE DATABASE` 必然撞车；`-Fc` + `-d casdoor` 绕开。

#### 回滚

备份目录里有两份东西（`casdoor.dump` 逻辑备份、`pgdata-16.tgz` 整卷物理备份），任意一份都能救。
**镜像要同时改回 16**——16 的数据目录 17 读不了，只回滚数据没有意义：

```bash
COMPOSE_DIR=/opt/mybmo-mcp/deploy/casdoor
BK=/root/casdoor-pg-upgrade/<STAMP>
VOL=mybmo-casdoor_pgdata

cd $COMPOSE_DIR
git checkout <升级前的提交> -- docker-compose.yml     # 镜像回 16
docker compose down
docker volume rm $VOL && docker volume create $VOL
docker run --rm -v $VOL:/to -v $BK:/backup alpine \
  sh -c 'cd /to && tar xzf /backup/pgdata-16.tgz'
docker compose up -d
```

验证通过后可以删 tar 包；`casdoor.dump` 建议长期留着——Casdoor 的账号、应用、自定义
scope 都在里面，比在管理台里重建省事。

## 八、Casdoor 与新增客户端

Casdoor 已在同机运行，`MYBMO_AUTH_MODE=oidc`，身份来自 JWT（详见
[`casdoor-notes.md`](casdoor-notes.md)）。三件事需要长期维护。

### 8.1 新增一个客户端

**没有自助注册。** Casdoor 的 DCR 不可用——没有回调白名单、没有限流，
而且它签出的令牌 `aud` 是客户端自己的 client_id，不在白名单里就调不动本服务
（[ADR 0007](decisions/0007-no-dcr.md)）。

```text
1. Casdoor 控制台 → Applications → 新建
2. Redirect URL 填该客户端的固定回调（例：https://claude.ai/api/mcp/auth_callback）
3. 只勾 authorization_code + refresh_token，不要勾 password
4. 记下 Client ID
5. deploy/.env：MYBMO_OIDC_AUDIENCE=<原 id>,<新 id>
6. docker compose -f deploy/docker-compose.yml up -d mcp
```

漏掉第 5 步的表现：客户端能登录、能拿到令牌，但每个请求都 401。

### 8.2 不可逆的事

> **issuer 定死就是 `https://auth.pandabase.top`，之后不能改。**
> 它写进每个 JWT 的 `iss`，改一次 = 所有令牌失效 + 所有客户端重新配置。

### 8.3 切回 local 模式（应急）

`oidc` 模式下 Caddy **不能**再叠 basic auth——它会在令牌到达容器前拦下请求，
表现是永远 `401 Basic realm`。所以进出 oidc 要同时改两处：

```bash
# 切到 oidc：deploy/.env 设 MYBMO_AUTH_MODE=oidc + CASDOOR_ISSUER +
#            MYBMO_OIDC_AUDIENCE + MYBMO_JWKS_URL + MYBMO_RESOURCE_URL
#            /etc/caddy/Caddyfile 去掉 basic_auth
docker compose -f deploy/docker-compose.yml up -d --force-recreate
sudo systemctl reload caddy

# 切回 local：deploy/.env 设 MYBMO_AUTH_MODE=local，Caddyfile 加回 basic_auth
docker compose -f deploy/docker-compose.yml up -d --force-recreate
sudo systemctl reload caddy
```

切回前先建 `data/tenancy.json` 是不必要的（local 模式不看它），
但**切到 oidc 前必须有**，否则所有调用报"缺少授权表"——这是有意的，
理由见 README 的三条硬规则。

## 九、常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| Caddy 日志报 ACME 失败 | 子域没有 A 记录 / 80 端口未放通 | `dig +short baiduads.pandabase.top`，再查安全组 |
| `/mcp` 一律 `401` | 没带 Bearer 令牌 / 令牌过期 / `aud` 不在白名单 | 先看 `WWW-Authenticate` 里的 `error`：`invalid_token` 查 `MYBMO_OIDC_AUDIENCE` 与 `MYBMO_JWKS_URL`；`insufficient_scope` 查 Casdoor 应用里的 scope 登记（casdoor-notes 第四节） |
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
| 身份模式 | **`oidc`**（2026-11-08 12:10 切换），Casdoor `auth.pandabase.top` |
| 鉴权 | 只走 OAuth bearer；**Caddy 的 basic_auth 已移除**（原因见下） |
| 镜像 | `mybmo-mcp:0.1.0`，258 MB |
| 数据库 | **PostgreSQL 17.11**（`17-bookworm`，2026-11-08 13:32 由 16 迁上，见 7.1） |
| 内存占用 | mcp 61 MiB / PG 63 MiB / Casdoor 22 MiB，宿主 Caddy 约 30 MiB，整机 1967 MiB 可用 1236 MiB |
| 构建耗时 | 首次 13.5 分钟（已配腾讯云镜像加速；未配时 ghcr 单层就要 745 秒） |

已验证：`/healthz` 200、无凭据 `/mcp` 401、带凭据 400（握手拒绝空 body）、
MCP 客户端列出 5 个工具、`search_capabilities("报表")` 命中 3 条。

**踩过的坑**（都已在代码里修掉，留档避免复发）：

0. **切到 oidc 后必须同时去掉 Caddy 的 `basic_auth`。** 两者叠加的结果是：
   OAuth 令牌根本到不了容器，客户端永远收到 `401 Basic realm="restricted"`，
   看起来像"OAuth 不生效"，实际是代理层先拦了。实测踩到过。

1. `MCP_PASSWORD_HASH` 含 `$`，写进 compose 的 `environment: ${VAR}` 会被当变量插值 →
   改为 `env_file` 或宿主 Caddyfile。共享宿主机形态下根本不该写进 `.env`。
2. `ghcr.io/astral-sh/uv` 基础镜像在境内拉取极慢 → Dockerfile 改用 `python:3.12-slim` + pip 装 uv。
3. `psycopg` / `pyjwt` 当前零引用却每次构建都要下载 → 移到 `optional-dependencies.p4`。
4. **通配符不等于有记录**：DNSPod 上 apex 有 A 记录，`baiduads` / `auth` 必须显式添加。

## 十一、还没验证的

- **Docker vs systemd**：本文按 Docker 写。若目标机不允许装 Docker，
  需另写 systemd unit，`MYBMO_DATA_DIR` 指向 `/var/lib/mybmo`，其余不变
- **备份恢复演练**：当前无已绑定账户，恢复验证无意义，等首个账户绑定后补
- **Claude Desktop 直连**：OAuth 链路已通（8.1 可注册），但它的授权码 + PKCE 全流程
  还没用真实客户端跑过（tasks 5.9）
- **PG 17 的中文排序**：新集群 locale 是 `C.UTF-8`（码点序，不是拼音序）。当前没有中文
  `user_name`，`ILIKE` 与排序的实际表现要等真账户进来才能验
