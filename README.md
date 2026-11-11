<p align="center"><img src="assets/logo.png" alt="mybmo-mcp" width="160"></p>

# mybmo-mcp

百度营销开放平台的 MCP 能力网关。**389 个接口收进 5 个工具**——用检索代替枚举，
模型不必面对 389 个工具定义；百度令牌只活在服务端，客户端拿的是本服务自己的身份。

> 非官方项目，与百度无隶属关系。需自行在 <https://dev2.baidu.com> 创建应用并完成授权。

部署、身份与数据库在 [`pandabase-infra`](https://github.com/bianzhishui/pandabase-infra)，本仓库只管业务。
线上实例：`https://baiduads.pandabase.top/mcp`（OAuth Bearer，令牌由 `auth.pandabase.top` 签发）。

## 快速开始

```bash
uv sync
cp .env.example .env
python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"  # 填进 MYBMO_MASTER_KEY
uv run mybmo-mcp --help
```

默认 stdio，由 MCP 宿主作为子进程拉起：

```json
{
  "mcpServers": {
    "mybmo": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/mybmo-mcp", "mybmo-mcp"]
    }
  }
}
```

没有百度账号也能用：`search_capabilities` / `get_capability_schema` 只读随包的接口元数据，
不碰凭据、不联网。

### 绑定百度账户

```bash
uv run mybmo-mcp auth bind --alias main        # 打出授权链接，state 15 分钟一次性
```

浏览器打开链接授权后，百度跳回 `MYBMO_BAIDU_CALLBACK_URL`
（默认实例：`https://baiduads.pandabase.top/oauth/baidu/callback`），由**运行中的服务**
落库——所以 http 模式下先起服务再 bind。回调地址必须与 dev2 应用「安全设置」里登记的
**根域**一致（百度按根域比对），且不能带 `?` 或 `%`。

## 部署（公网）

本仓库自带 `deploy/docker-compose.yml`，只跑 mcp 一个服务，加入 infra 的 `pandabase` 网络：

```bash
cd deploy
cp .env.example .env      # 填百度凭据、主密钥；oidc 的五个值见 docs/auth-contract.md
docker compose up -d --build
curl -fsS https://baiduads.pandabase.top/healthz
```

镜像仍由本仓库构建（Dockerfile 打的是 Python 产物）；网络、Casdoor、PostgreSQL、Caddy、
证书与备份由 [`pandabase-infra`](https://github.com/bianzhishui/pandabase-infra) 提供。
**反代不做鉴权**，`/mcp` 的令牌校验必须在本进程内完成。

## 五个工具

| 工具 | 作用 |
|---|---|
| `search_capabilities` | 中文或英文检索能力，返回短摘要（`拓词` → `KRService.getKRByQuery`） |
| `get_capability_schema` | 取完整参数树：字段名、类型、单位、层级，含手写说明 |
| `execute_capability` | 执行任意接口。参数自动对齐驼峰，未知字段直接拒绝并给候选 |
| `list_accounts` | 当前身份可见的百度账户（别名、中文名、是否可用） |
| `auth_status` | 令牌有效期、是否需要重新授权 |

**本服务只提供能力，不做业务编排，也不拦写操作。** 拓词、报表汇总、写前确认属于
调用方（skill / agent 工作流）的职责，理由见 [ADR 0003](docs/decisions/0003-capability-only.md)。

## 三条硬规则

1. **多用户模式下授权表缺失 → 直接报错。** 回退成"人人可用"的代价远大于停机。
2. **不传 `account` → 只在当前身份唯一可用账户时生效。** 多个候选就报错，
   绝不落到进程级默认账户上——多用户下它极可能属于别人。
3. **越权在发请求之前被拒。** 授权判定发生在取凭据那一步，越权的调用不会产生网络请求。

授权表格式见 [`tenancy.example.json`](tenancy.example.json)，档位与账户模型见
[ADR 0004](docs/decisions/0004-multi-tenant-accounts.md)。

## 两种运行模式

| 模式 | 场景 | 身份 |
|---|---|---|
| `local`（默认） | 本机 stdio，单用户 | 进程即身份，不校验令牌 |
| `oidc` | 公网 HTTP，多客户端 | 校验 Bearer JWT（签名 / iss / aud / exp / scope），身份来自 `sub` |

`oidc` 模式需要 infra 提供的 5 个值（issuer、JWKS、audience 等），
见 [`docs/auth-contract.md`](docs/auth-contract.md)。**配错的表现是全量 401**，
所以那份契约里的变量表要对着抄。

## 当前状态

线上以 `oidc` 模式运行，授权码 + PKCE 全流程已用真实令牌验证；测试全绿（离线）。

**已有一个真实百度账户完成绑定并跑通只读能力**（2026-11-11）：`list_accounts` 返回中文名、
`getCampaign` 返回 `[]`、`getKRByQuery` 单次上限实测 300 条、错误 `userName` 实测 81020。
未验证的是**写操作与报表分页**（账户内暂无计划与消费数据），结论记在
[`docs/baidu-behavior.md`](docs/baidu-behavior.md)。

待办与验收标准在 [`openspec/`](openspec/)：`live-validation` 是 v1 的唯一阻塞项，
`multi-tenant-persistence` 要等出现第二个真实用户才启动。

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | 三层信任边界、关键时序、数据模型、凭据生命周期 |
| [`docs/auth-contract.md`](docs/auth-contract.md) | **与 infra 的唯一契约**：鉴权不变量、环境变量、端点、卷布局 |
| [`docs/baidu-behavior.md`](docs/baidu-behavior.md) | 百度接口行为实测结论；未验证项不得写进能力说明 |
| [`docs/reports.md`](docs/reports.md) | 推广报告速查：三个方法、通用参数、reportType 全表、列字典、实测对照 |
| [`ADR 0002`](docs/decisions/0002-tech-stack.md) | 技术栈与理由 |
| [`ADR 0003`](docs/decisions/0003-capability-only.md) | 只做能力层，不做业务编排与写审批 |
| [`ADR 0004`](docs/decisions/0004-multi-tenant-accounts.md) | 多租户账户模型、MCC 子账户、`wait` 语义 |
| [`ADR 0005`](docs/decisions/0005-sdk-poc.md) | `baiduads-sdk` 可行性验证：为什么只做元数据源 |
| [`.env.example`](.env.example) | 全部环境变量与取值说明 |

接口元数据分两层，不要混：`baidu/capabilities.json` 由
[`scripts/build_capabilities.py`](scripts/build_capabilities.py) 内省 SDK 生成（**不要手改**）；
`baidu/descriptions.json` 是人写的能力说明，加载时合并进注册表与 JSON Schema，
写错能力名或参数路径会直接报错。

## 许可

待定（Apache-2.0 或 MIT）。依赖的 MCP Python SDK 为 MIT；基础设施仓库依赖的 Casdoor 为 Apache-2.0。
