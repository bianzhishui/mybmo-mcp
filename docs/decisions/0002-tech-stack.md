# ADR 0002：技术栈选型

- 状态：已接受
- 日期：2026-11-07
- 决策者：项目负责人
- 关联：[ADR 0001](0001-auth-architecture.md)（鉴权架构）

## 背景

ADR 0001 定了「自部署 Casdoor + 本服务作 OAuth 2.1 Resource Server」，
但没有回答三个落地问题：**Python 侧用哪些包、数据库用哪个、要不要在服务前加一层 nginx**。

本文把这三层钉死，并记录被否决的方案与理由，避免后续反复讨论。
文中版本号为 2026-11-07 在 PyPI 与临时虚拟环境中实测所得，最终以 `uv.lock` 为准。

## 决策一览

| 层 | 选型 | 版本 |
|---|---|---|
| 语言 | Python | 3.12（`.python-version`） |
| 包管理 | uv + `uv_build` | 0.9.x |
| MCP 框架 | 官方 `mcp`（`MCPServer`） | 2.3.0 |
| 出站 HTTP | `httpx2` | 2.16.0 |
| 数据库 | PostgreSQL | 17 |
| 驱动 | `psycopg[binary,pool]`（异步） | 3.3.8 |
| SQL / 迁移 | 手写 SQL + `migrations/*.sql` | 无 ORM |
| JWT 校验 | `pyjwt[crypto]` | 2.15.1 |
| 对称加密 | `cryptography`（AES-GCM） | 随 lock |
| 配置 | `pydantic-settings` | 2.16.2 |
| CLI | `typer` | 0.28.0 |
| 测试 | `pytest` + `pytest-asyncio` | 1.4.x |
| 反向代理 | Caddy | 2.x |
| 授权服务器 | Casdoor（Docker） | 见 ADR 0001 |

## D1. MCP 框架用官方 `mcp`，不用 `fastmcp`

官方 SDK 在 v2（2026-10-02 发布 2.3.0）把 `FastMCP` 换成了 `MCPServer`，
并且**内置了资源服务器所需的能力**，这是过去必须依赖 FastMCP 的唯一理由，现已消失。

实测确认存在的接口（临时环境 `uv run --with mcp==2.3.0`）：

```python
from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.middleware.auth_context import get_access_token

MCPServer.run(transport="stdio" | "sse" | "streamable-http")

AuthSettings 字段: issuer_url, resource_server_url, required_scopes,
                   validate_token_resource, client_registration_options
AccessToken 字段: token, client_id, scopes, expires_at, resource, subject, claims
TokenVerifier 唯一方法: async verify_token(token) -> AccessToken | None
```

SDK 会自动发布 RFC 9728 的 `/.well-known/oauth-protected-resource/mcp`，
未认证请求返回 401 且 `WWW-Authenticate` 指向该文档——正是 `docs/architecture.md` 第二节要求的形状。

**我们需要自己写的只有 `TokenVerifier.verify_token`**：用 `pyjwt` 的 `PyJWKClient`
取 Casdoor 的 JWKS，校验签名 / `iss` / `aud` / `exp` / `scope`，
把 `sub` 与 `tenant_id` 放进 `AccessToken.claims`。

否决理由（独立 `fastmcp` 4.1.6）：

1. 它不建在官方 SDK 上。其核心包 `fastmcp-slim` 只依赖 `mcp-types` / `pydantic` / `rich`，
   等于自己重实现了一遍 server 运行时。选它意味着协议层由第三方维护。
2. 它对本项目真正的增值只剩一个 `JWTVerifier` 类，为这一个类引入一整套框架不划算。
3. 大版本迭代快（2.x → 3.x → 4.x），对要长期无人值守运行的服务是迁移税。

**风险**：2.3.0 发布仅五周，生态示例多停留在 v1。缓解：lock 精确锁定；
若遇到阻塞问题，两者同用 `mcp-types` 2.x、装饰器写法接近，换 `fastmcp` 属于机械替换而非重构。

## D2. 出站 HTTP 客户端用 `httpx2`

- `httpx` 主线停在 **0.28.1（2024-12-06）**，近两年无新版。
- `httpx2` **2.16.0（2026-10-21）**，仓库已迁至 `github.com/pydantic/httpx2`，是事实上的 v2。
- `mcp` 2.3.0 的核心依赖已包含 `httpx2>=2.10.0`，**它本来就在依赖树里**。
  再用 `httpx` 会同时安装两套 HTTP 栈。

测试用 `httpx2.MockTransport` 注入（已验证存在），不引 `respx`。

## D3. 数据库只支持 PostgreSQL 17

曾评估「一个 URL 切 SQLite / MySQL / PostgreSQL」，最终否决。

否决理由：三库支持的代价是退到方言交集，必须放弃 `timestamptz`、`JSONB`、
`RETURNING`、`pg_advisory_xact_lock`、部分索引、`ON CONFLICT`，
而这几个恰好是本项目最关键语义的载体（见 D4、D5）。
收益只有一个：别人拿现成 MySQL 来部署。规模不匹配。

版本选 **17** 而非 18：Casdoor 官方 compose 仍以 17 为主，2 核 2G 上两者无性能差异，
选稳的。镜像用 `postgres:17-bookworm`（非 alpine），因为 alpine 的 musl 中文 locale 支持不完整，
`user_name` 排序与 `ILIKE` 会与 Debian 版产生差异。

两个库共用同一实例：`mybmo`（业务）与 `casdoor`（Casdoor 自用）。
备份是三条命令：`pg_dumpall --globals-only` + 两个库各一次 `pg_dump -Fc`。

## D4. 手写 SQL，不引 ORM

全项目 6 张表。最关键的语句是审批的一次性消费：

```sql
UPDATE mybmo_approvals
   SET used_at = now()
 WHERE id = :id
   AND used_at IS NULL
   AND expires_at > now()
   AND account_id = :account_id
   AND payload_hash = :payload_hash
RETURNING id;
```

「哈希一致 + 未使用 + 未过期 + 归属正确」四个条件必须在**一条语句内原子完成**。
拿到返回行才放行。ORM 会把这层语义藏进会话状态，反而降低可审查性。

同时项目约束要求「所有查询带 `tenant_id`，用仓储层封装而非散落的 if 判断」。
手写 SQL 让每个查询的租户条件都能肉眼审查，这是选它而非 ORM 的理由。

**刷新锁**：`pg_advisory_xact_lock(hashtextextended(:account_id::text, 0))`。
用 `hashtextextended`（返回 int8）而非 `hashtext`（返回 int4），避免账户增多后哈希碰撞。
事务结束自动释放，不会因连接泄漏而卡死。

## D5. 迁移用 SQL 文件 + `schema_version` 表

`migrations/NNN_description.sql` + 一张记录已应用版本的元数据表，约 40 行 runner，
通过 `mybmo-mcp db migrate` 执行。

不引 Alembic 的理由：单方言、表数少、部署者只有一人，
autogenerate 的价值抵消不了多一层抽象的成本。
**切换条件**：表数超过约 15 张，或需要多人并行改 schema 时，再引 Alembic（psycopg 可直接对接）。

## D6. 反向代理用 Caddy，不用 nginx

`docs/architecture.md` 已按 Caddy 写，本文确认不改变。

1. **流式响应**。MCP 的 Streamable HTTP 依赖即时刷新；nginx 默认 `proxy_buffering on`
   会把事件攒在缓冲区，表现为「连上了但一动不动」，必须显式关闭并调 `proxy_read_timeout`。
   Caddy 的 `reverse_proxy` 默认不缓冲。
2. **证书**。Caddy 自动申请续期，配置 5 行；nginx 需要 certbot + cron + reload，漏配就是一次事故。
3. **单二进制**，2 核 2G 机器上常驻约 30MB。

三条硬约束：

- **反代不做鉴权**。让 Caddy 注入 `X-User` 头、应用信任它，等于留了一条直连回环地址即可伪造的旁路。
  鉴权只在应用层（`token_verifier`）做。
- **反代不做限流兜底**。Caddy 标准构建无 `rate_limit`（需 xcaddy 编译）。
  防 DCR 滥用靠 Casdoor 的 `dcrPolicy` + 回调白名单，其次才是云 WAF。
- **不叠两层代理**。要 nginx 就顶掉 Caddy，不要串起来。

## D7. 凭据加密统一走 AES-GCM，不用 `keyring`

`design.md` D5 原写「本地开发用系统 keyring」，本文改为**统一 AES-GCM**。

理由：keyring 在无头 Linux 需要 SecretService 守护进程，macOS 会弹授权框，CI 里经常不可用。
两套实现意味着两条测试路径，而其中一条在容器里根本跑不起来。

- 本地 / stdio：加密文件，权限 `600`，密钥来自 `MYBMO_MASTER_KEY`
- 服务端 / 多租户：PG `bytea` 列存密文，每租户独立 DEK，主密钥仍来自环境变量 / KMS

## D8. 不引 Redis

单机单副本，刷新并发用 PG advisory lock 已解决。
Redis 只增加一个要备份、要监控、会独立故障的组件。
**切换条件**：需要横向扩副本，或 accessToken 缓存成为瓶颈时。

## 后果

### 正面

- 依赖面小：核心 7 个包，其中 `httpx2` 与 `pyjwt` 本就是 `mcp` 的传递依赖
- 协议层由官方维护，鉴权边界（`TokenVerifier`）由自己掌握，便于审计
- 数据库只有一种方言，SQL 可逐条审查，无「本地能跑、生产报错」的隐患
- 部署栈只有 Casdoor / PG / server / Caddy 四个容器

### 负面 / 需要承担

| 风险 | 说明 | 缓解 |
|---|---|---|
| `mcp` v2 太新 | 2026-10 发布，生态示例少 | lock 精确锁定；保留切 fastmcp 的退路 |
| 不支持 MySQL | 已有 MySQL 的用户需另起 PG | 明确写入 README 的前置条件 |
| 无 ORM | 仓储层手写 SQL 量大 | 表少；用 `TypedDict`/dataclass 做行映射 |
| SQLite 不支持 | 无法「双击运行」 | 单机场景用 docker compose 起 PG |
| 单机无 HA | PG 或 Casdoor 挂了即不可用 | 明确标注单点，后续再谈多副本 |

## 未决问题

- [ ] `mcp` 2.3.0 在 P1–P3 实测中是否有阻塞缺陷（决定是否回退到 fastmcp）
- [ ] Casdoor 版本锁定策略：镜像 tag 还是 digest
- [ ] 生产是否启用 PG 逻辑复制到备机（当前判断：暂不需要）
- [ ] **部署形态：容器还是 systemd 原生**。目标机（49.232.229.71）未装 Docker、内存 1.9Gi，
      四容器方案在该规格上偏紧，P7 前需实测内存占用再定；若走原生，
      Casdoor 用单二进制、PG 用系统包、服务用 systemd unit
