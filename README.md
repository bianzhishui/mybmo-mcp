# mybmo-mcp

百度营销开放平台的 MCP 能力网关。**389 个接口收进 5 个工具**——用检索代替枚举，
模型不必面对 389 个工具定义；百度令牌只活在服务端，客户端拿的是本服务自己的身份。

> 非官方项目，与百度无隶属关系。需自行在 <https://dev2.baidu.com> 创建应用并完成授权。

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

公网接入：`uv run mybmo-mcp -t streamable-http --host 127.0.0.1 -p 8000`。
默认只监听回环，**不要**在没有鉴权的情况下直接暴露到公网，原因见下文"三条硬规则"。

已上线实例：**`https://baiduads.pandabase.top/mcp`**（basic auth，当前 `MYBMO_AUTH_MODE=local`）。
部署、备份、回滚、Casdoor 切换见 [`docs/operations.md`](docs/operations.md)。

没有百度账号也能用：`search_capabilities` / `get_capability_schema` 只读随包的接口元数据，
不碰凭据、不联网。

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

部署前必须知道，这些都是刻意的默认拒绝，别改回去：

1. **多用户模式下授权表缺失 → 直接报错。** 回退成"人人可用"的代价远大于停机。
2. **不传 `account` → 只在当前身份唯一可用账户时生效。** 多个候选就报错，
   绝不落到进程级默认账户上——多用户下它极可能属于别人。
3. **越权在发请求之前被拒。** 授权判定发生在取凭据那一步，越权的调用不会产生网络请求。

授权表格式见 [`tenancy.example.json`](tenancy.example.json)，档位与账户模型见
[ADR 0004](docs/decisions/0004-multi-tenant-accounts.md)。

## 当前状态

P0–P3 已完成，176 个测试离线全绿：能力网关、账户与凭据、归属校验、调用审计都可验。
P7 已上线（`/healthz` 200、TLS 已签发、客户端能列出 5 个工具），P4（Casdoor）未开工。

**唯一未验证的是真实百度接口**——没有可用账号，所以 `execute_capability` 从未真正
打到 `api.baidu.com`。拿到 AppID 后跑一次绑定就能确认。

逐项清单在 [`tasks.md`](openspec/changes/bootstrap-mcp-server/tasks.md)，
技术取舍在 [`design.md`](openspec/changes/bootstrap-mcp-server/design.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | 三层信任边界、关键时序、数据模型、部署拓扑、安全清单 |
| [`docs/operations.md`](docs/operations.md) | 部署、TLS、备份恢复、升级回滚、Casdoor 切换清单、故障排查 |
| [`ADR 0001`](docs/decisions/0001-auth-architecture.md) | 为什么自部署 Casdoor 作授权服务器 |
| [`ADR 0002`](docs/decisions/0002-tech-stack.md) | 技术栈选型与理由（含被否决的方案） |
| [`ADR 0003`](docs/decisions/0003-capability-only.md) | 从"业务工具"转向能力网关，以及为什么不拦写操作 |
| [`ADR 0004`](docs/decisions/0004-multi-tenant-accounts.md) | 多租户账户模型、MCC 子账户、`wait` 语义 |
| [`ADR 0005`](docs/decisions/0005-sdk-poc.md) | `baiduads-sdk` 可行性验证：为什么只做元数据源 |
| [`.env.example`](.env.example) | 全部环境变量与取值说明 |

接口元数据分两层，不要混：`baidu/capabilities.json` 由
[`scripts/build_capabilities.py`](scripts/build_capabilities.py) 内省 SDK 生成（**不要手改**）；
`baidu/descriptions.json` 是人写的能力说明，加载时合并进注册表与 JSON Schema，
写错能力名或参数路径会直接报错。

## 许可

待定（Apache-2.0 或 MIT）。依赖的 Casdoor 为 Apache-2.0，MCP Python SDK 为 MIT。
