# ADR 0002：技术栈选型

- 状态：已接受
- 日期：2026-11-07
- 关联：[ADR 0003](0003-capability-only.md)、[ADR 0005](0005-sdk-poc.md)

## 决策一览

| 层 | 选型 | 版本 |
|---|---|---|
| 语言 | Python | 3.12（`.python-version`） |
| 包管理 | uv + `uv_build` | 0.9.x |
| MCP 框架 | 官方 `mcp`（`MCPServer`） | 2.3.0 |
| 出站 HTTP | `httpx2` | 2.16.0 |
| JWT 校验 | `pyjwt[crypto]` | 2.15.x |
| 对称加密 | `cryptography`（AES-GCM） | 随 lock |
| 配置 | `pydantic-settings` | 2.16.x |
| CLI | `typer` | 0.28.x |
| 测试 | `pytest` + `pytest-asyncio` | 1.4.x |
| Lint | `ruff` | 0.9+ |

`psycopg` 是 `optional-dependencies.p4` 里的未启用依赖——多租户落库还没做，见 D3。

## D1. MCP 框架用官方 `mcp`，不用 `fastmcp`

官方 SDK 在 v2 把 `FastMCP` 换成 `MCPServer`，并内置资源服务器所需能力（`TokenVerifier`、
`AuthSettings`、RFC 9728 元数据、401 + `WWW-Authenticate`）——这是过去必须依赖 FastMCP
的唯一理由，现已消失。

否决独立 `fastmcp`：它不建在官方 SDK 上，核心包只依赖 `mcp-types`/`pydantic`，
等于自己重实现一遍运行时；对本项目的增值只剩一个 `JWTVerifier` 类；大版本迭代快。

**风险**：2.3.0 生态示例少。缓解：lock 精确锁定；两者同用 `mcp-types`，换回去属于机械替换。

## D2. 出站 HTTP 客户端用 `httpx2`

`httpx` 主线停在 0.28.1（2024-12），`httpx2` 是事实上的 v2 且仓库已迁至 pydantic 组织。
`mcp` 2.3.0 本身依赖 `httpx2>=2.10`，再引 `httpx` 会同时装两套 HTTP 栈。
测试用 `httpx2.MockTransport`，不引 `respx`。

## D3. 手写 SQL，不引 ORM

落库时表很少（目标模型 5 张，见 [`docs/architecture.md`](../architecture.md) 第四节）。
最关键的一类语句是「单条 UPDATE 完成的多条件判定」，ORM 会把这层语义藏进会话状态，
反而降低可审查性。项目约束要求「所有查询带 `tenant_id`」，手写 SQL 让每个租户条件都能肉眼审查。

**切换条件**：表数超过约 15 张，或需要多人并行改 schema 时，再考虑 ORM。

## D4. 迁移用 SQL 文件 + `schema_version` 表

`migrations/NNN_description.sql` + 一张记录已应用版本的元数据表，约 40 行 runner。
不引 Alembic：单方言、表数少、部署者只有一人，autogenerate 的价值抵消不了多一层抽象。

库与角色由基础设施创建，本仓库只跑自己的迁移。

## D5. 凭据加密统一走 AES-GCM，不用 `keyring`

`keyring` 在无头 Linux 需要 SecretService，macOS 会弹授权框，CI 里经常不可用；
两套实现意味着两条测试路径，而其中一条在容器里跑不起来。

- 现在：加密文件，权限 `600`，密钥来自 `MYBMO_MASTER_KEY`
- 落库后：PG `bytea` 列存密文，每租户独立 DEK，主密钥仍来自环境变量 / KMS

## 后果

### 正面

- 依赖面小：运行时 7 个包，其中 `httpx2` 与 `pyjwt` 本就是 `mcp` 的传递依赖
- 协议层由官方维护，鉴权边界（`TokenVerifier`）自己掌握，便于审计

### 负面

| 风险 | 说明 | 缓解 |
|---|---|---|
| `mcp` v2 太新 | 生态示例少 | lock 精确锁定；保留切 `fastmcp` 的退路 |
| 无 ORM | 落库后手写 SQL 量大 | 表少；用 `TypedDict`/dataclass 做行映射 |
| 依赖 IdP 的正确配置 | `aud`/JWKS 配错就是全量 401 | [`docs/auth-contract.md`](../auth-contract.md) 写明不变量 |

## 未决问题

- [ ] 多租户落库（`psycopg` 启用时机）—— 取决于是否真的出现第二个用户
- [ ] `mcp` 2.3.0 在真实流量下是否有阻塞缺陷（决定是否回退 `fastmcp`）
