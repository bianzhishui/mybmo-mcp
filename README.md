# mybmo-mcp

百度营销开放平台的 MCP 服务：把账户、关键词、投放与报表能力封装成 AI 可直接调用的工具，
并用**自部署 Casdoor** 承担公网接入鉴权，用**服务端凭据库**管理多个百度营销账户。

> 非官方项目，与百度无隶属关系。本项目通过百度营销开放平台公开接口工作，
> 使用者需自行在 <https://dev2.baidu.com> 创建应用并完成授权。

## 一、这个项目解决什么

百度营销 API 有 399 个接口、每接口一套请求包装类，且**每个账户都有一套独立令牌**。
直接把它包成 MCP 会遇到三个具体问题：

| 问题 | 后果 | 本项目的做法 |
|---|---|---|
| 接口太多 | 逐个注册成工具会让模型选错、上下文爆炸 | 8–12 个高频强类型工具 + 1 个目录查询 + 1 个只读透传 |
| 多账户 | 令牌、账户名、别名混在一起，容易串号 | 服务端账户注册表 + 租户归属校验，模型只能选服务端给它的账户 |
| 公网暴露 | 百度 accessToken 被当成登录凭证，受众校验失效 | MCP 与百度鉴权**分两层**，令牌绝不混用 |

核心一句话：**MCP 客户端拿的是 Casdoor 签发给本服务的令牌，百度令牌只活在服务端。**

## 二、鉴权架构

```text
┌──────────────┐   ① OAuth 2.1 + PKCE + DCR/CIMD
│ MCP 客户端    │ ─────────────────────────────┐
│ Claude/Cursor│                              │
└──────────────┘                              ▼
                              ┌───────────────────────────────┐
                              │ Casdoor（自部署，授权服务器）    │
                              │ 签发 aud=mybmo-mcp 的短时令牌   │
                              └───────────────────────────────┘
                                              │ ② JWT
                                              ▼
┌──────────────────────────────────────────────────────────────┐
│ mybmo-mcp（OAuth 2.1 Resource Server）                        │
│   · 校验签名 / iss / aud / exp / scope                        │
│   · sub + tenant_id → 查询可访问的百度账户（不信模型参数）        │
│   · 取 refreshToken → 刷新 accessToken → 注入 userName        │
└──────────────────────────────────────────────────────────────┘
                                              │ ③ 百度协议
                                              ▼
                              ┌───────────────────────────────┐
                              │ 百度营销开放平台                 │
                              │ api.baidu.com / u.baidu.com   │
                              └───────────────────────────────┘
```

三层职责边界：

| 层 | 谁负责 | 不负责 |
|---|---|---|
| 接入鉴权 | Casdoor 签发，本服务校验 | 不签发用户令牌，不信任百度令牌 |
| 账户绑定 | 本服务（百度 OAuth 回调 + 凭据库） | 不做用户登录 |
| 写操作审批 | 本服务（preview → approval → execute） | 不用 MCP 工具注解代替拦截 |

细节见 [`docs/architecture.md`](docs/architecture.md)，
选型理由见 [`docs/decisions/0001-auth-architecture.md`](docs/decisions/0001-auth-architecture.md)。

## 三、工具设计

不做 399 个工具。首批按"业务闭环"划：

| 分类 | 工具 | 说明 |
|---|---|---|
| 账户 | `list_accounts` | 当前身份可访问的百度账户 |
| 账户 | `account_snapshot` | 余额、预算、状态、当日消耗 |
| 账户 | `auth_status` | 令牌有效期、是否需要重新授权 |
| 只读 | `keyword_plan` | 种子词拓词 + 推荐出价 + 竞争度 |
| 只读 | `query_report` | 按日/单元/关键词拉报表 |
| 只读 | `get_entity` / `list_entities` | 计划、单元、关键词、创意读取 |
| 发现 | `api_describe` | 查接口方法、参数、是否可写 |
| 透传 | `api_read` | 仅只读白名的长尾接口 |
| 写入 | `change_preview` | 生成完整报文 + `approval_id` |
| 写入 | `change_execute` | 校验 `approval_id` 后执行 |
| 资源 | `baidu://accounts`、`baidu://catalog` | 上下文资源，不进工具列表 |

写接口**不做通用透传**。任何写操作必须走 `change_preview` → 用户确认 → `change_execute`。

## 四、目录结构

```text
mybmo-mcp/
├── src/mybmo_mcp/
│   ├── server.py           # MCP 入口，stdio / streamable-http
│   ├── tools/              # 按工具域拆：accounts / keyword / report / write
│   ├── baidu/
│   │   ├── transport.py    # 百度网关请求、重试策略、错误归一化
│   │   ├── services/       # 账户、关键词、报表、投放
│   │   └── catalog.py      # 接口目录（Service/Method/读写/参数）
│   ├── auth/
│   │   ├── oauth.py        # 百度 OAuth：授权码、刷新、轮换
│   │   ├── vault.py        # 凭据加密存储（本地 keyring / KMS）
│   │   ├── resource.py     # MCP Resource Server：JWT 校验、audience
│   │   └── tenancy.py      # 用户、租户、账户授权关系
│   ├── approval/           # preview / digest / approval_id / TTL
│   └── audit/              # 调用与写入审计（四态）
├── deploy/                 # Casdoor + 服务端 compose、Caddy 配置
├── tests/                  # 离线优先，不打真实百度接口
├── docs/                   # 架构、决策记录、运维
└── openspec/               # 规格与变更提案
```

## 五、当前状态

**只有文档和骨架，尚无可用实现。** 已确定的是架构与边界，未验证的是真实接口字段。

| 项 | 状态 |
|---|---|
| uv 项目骨架 | ✅ |
| 鉴权架构决策 | ✅ 已定（Casdoor 自部署） |
| 契约文档 | ✅ `docs/` |
| 传输层 / 工具实现 | ⬜ 待开工 |
| 真实账户联调 | ⬜ 需要可用账号 |

待办清单在 [`openspec/changes/bootstrap-mcp-server/tasks.md`](openspec/changes/bootstrap-mcp-server/tasks.md)，
技术取舍在 [`design.md`](openspec/changes/bootstrap-mcp-server/design.md)。

## 六、路线图

| 阶段 | 目标 | 出口标准 |
|---|---|---|
| P0 | 骨架与契约 | `uv sync` 通过，`mybmo-mcp --help` 可运行 |
| P1 | 百度传输层 | 用脱敏 fixture 跑通请求构造与错误归一化 |
| P2 | 账户与凭据 | 多账户存取、别名隔离、刷新轮换，断网可预览 |
| P3 | 只读工具 | 账户快照、拓词、报表在真实账户上返回正确数据 |
| P4 | Casdoor 接入 | Claude / Cursor 贴 URL 可完成自助注册与登录 |
| P5 | 写操作审批 | preview → 确认 → execute 闭环，越权被拒 |
| P6 | 多租户与审计 | 跨租户账户不可见，写入可追溯 |
| P7 | 上线 | 腾讯云轻量 + Caddy + HTTPS，管理台不暴露公网 |

## 七、非目标

- 不做百度营销的官方 SDK 替代品，只覆盖投放日常所需
- 不做 399 个接口的全量工具映射
- 不保存用户百度账号密码，只保存 OAuth 令牌
- 不做竞价策略算法；出价建议只做接口返回的转述

## 八、许可

待定（Apache-2.0 或 MIT）。依赖的 Casdoor 为 Apache-2.0，MCP Python SDK 为 MIT。
