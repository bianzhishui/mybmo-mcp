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
| 能力执行 | 本服务（忠实执行 + 账户归属校验） | 不判断操作是否安全，不做业务编排 |

细节见 [`docs/architecture.md`](docs/architecture.md)，
选型理由见 [`docs/decisions/0001-auth-architecture.md`](docs/decisions/0001-auth-architecture.md)。

### 账户归属

**别名不是凭据。** 知道别人的 `axd` 不等于能用它：每次取账户都经过 `AccountAccess`，
先按身份筛出可见账户，再按档位判定。三个档位有顺序：`read < write < admin`。

| 配置 | 含义 |
|---|---|
| `MYBMO_AUTH_MODE=local`（默认） | stdio 单用户，本机进程即本人，对全部账户有 admin |
| `MYBMO_AUTH_MODE=oidc` | 多用户，身份来自 JWT，权限**只**来自 `data/tenancy.json` 的 grants |

两条默认拒绝，写在这里是为了让后来人别改回去：

- 多用户模式下授权表缺失 → 直接报错。回退成"人人可用"的代价远大于停机
- 不传 `account` → 只在当前身份**唯一**可用账户时生效。有多个候选就报错，
  绝不落到进程级默认账户上——多用户下它极可能属于别人

`tenancy.json` 里 `role` 之类字段不参与判定，所有权限只来自 `grants`，
没有隐含的租户管理员。示例见 [`tenancy.example.json`](tenancy.example.json)。

## 三、工具设计

**本项目只提供能力，不做业务编排。** 拓词、报表汇总、写前确认属于调用方（skill / agent 工作流）的职责，
不在这里实现。理由见 [`docs/decisions/0003-capability-only.md`](docs/decisions/0003-capability-only.md)。

常驻工具 5 个，数量与接口总数无关：

| 类型 | 工具 | 说明 |
|---|---|---|
| 能力 | `search_capabilities` | 按中文/英文关键词检索能力，覆盖 SDK 全部接口 |
| 能力 | `get_capability_schema` | 取某个能力的完整参数定义 |
| 能力 | `execute_capability` | 忠实执行；**不拦截写操作**，由调用方决定是否确认 |
| 账户 | `list_accounts` | 当前用户可访问的百度账户 |
| 账户 | `auth_status` | 令牌有效期、是否需要重新授权 |

两个要点：

- **上下文成本 O(1)**：接口从 389 增到 1000，工具定义不变，只有检索结果随目录增长
- **写操作不做服务端审批**：能力元数据会标 `mutating` / `destructive`，是否要求人工确认
  由 skill 工作流决定。生产环境可通过配置整体置为只读

## 四、目录结构

```text
mybmo-mcp/
├── src/mybmo_mcp/
│   ├── server.py           # MCP 入口，stdio / streamable-http
│   ├── tools/
│   │   ├── accounts.py     # list_accounts / auth_status（本项目自身能力）
│   │   ├── _result.py      # 统一响应信封
│   │   └── capability.py   # search / get_schema / execute（待建）
│   ├── baidu/
│   │   ├── catalog.py      # 接口目录，能力注册表的基础
│   │   ├── errors.py       # 错误码释义、凭据脱敏
│   │   └── transport.py    # 原生 HTTP fallback（SDK 不可用时启用）
│   ├── auth/               # 凭据加密、OAuth、刷新锁、账户注册表、绑定
│   └── audit/              # 调用审计（待建）
├── deploy/                 # Casdoor + 服务端 compose、Caddy 配置
├── tests/                  # 离线优先，不打真实百度接口
├── docs/                   # 架构、决策记录、运维
└── openspec/               # 规格与变更提案
```

## 五、当前状态

**P0 / P1 已归档，P2 的凭据部分保留并可复用。** 当前已按
[ADR 0003](docs/decisions/0003-capability-only.md) 清掉业务组合层与写审批，
正在往「能力层」重建：账户能力可用，能力网关（search / schema / execute）待建。

| 项 | 状态 |
|---|---|
| uv 项目骨架 | ✅ |
| 鉴权架构决策 | ✅ ADR 0001 |
| 技术栈选型 | ✅ ADR 0002 |
| 定位调整（能力层） | ✅ ADR 0003 |
| 多租户账户模型（租户共享 + MCC） | ✅ ADR 0004 |
| MCP 入口（stdio / streamable-http） | ✅ 可启动 |
| 账户工具（`list_accounts` / `auth_status`） | ✅ 已注册 |
| 接口目录（133 服务 / 399 接口） | ✅ 随包分发 |
| 凭据库（AES-GCM / 别名隔离 / 损坏检测） | ✅ 离线可验 |
| 百度 OAuth 绑定与令牌刷新 | ✅ 离线可验（`[live]` 待账号） |
| 原生 HTTP 传输层 | ✅ 离线可验（降级为 fallback） |
| 能力注册表（389 条 / 585 模型） | ✅ 构建期从 SDK 内省生成 |
| **能力网关（search / schema / execute）** | ✅ 五工具端到端跑通 |
| 账户归属校验（三档权限 / 默认拒绝） | ✅ 离线可验（`tests/test_tenancy.py`） |
| 调用审计（JSONL / 脱敏 / 四态） | ✅ 离线可验（`tests/test_audit.py`） |
| Casdoor 接入与多租户 | ⬜ 待开工（P4） |
| 真实账户联调 | ⬜ 需要可用账号 |

遗留说明：`v0.1-poc` tag 保留了「10 个业务工具 + 写审批」的旧形态，仅作参考，不再维护。

待办清单在 [`openspec/changes/bootstrap-mcp-server/tasks.md`](openspec/changes/bootstrap-mcp-server/tasks.md)，
技术取舍在 [`design.md`](openspec/changes/bootstrap-mcp-server/design.md)。

## 六、路线图

| 阶段 | 目标 | 出口标准 |
|---|---|---|
| P0 | 骨架与契约 | `uv sync` 通过，`mybmo-mcp --help` 可运行 |
| P1 | 百度传输层 | 用脱敏 fixture 跑通请求构造与错误归一化 |
| P2 | 账户与凭据 | 多账户存取、别名隔离、刷新轮换，断网可预览 |
| P3 | 能力网关 | search → schema → execute 全链路，用 fixture 可跑通 |
| P4 | Casdoor 接入 | Claude / Cursor 贴 URL 可完成自助注册与登录 |
| P5 | （原写操作审批，已取消） | 见 ADR 0003：审批由 skill 工作流承担 |
| P6 | 多租户与审计 | 跨租户账户不可见，操作可追溯 |
| P7 | 上线 | 腾讯云轻量 + Caddy + HTTPS，管理台不暴露公网 |

## 七、非目标

- 不做业务编排：拓词、报表汇总、写前确认属于 skill 工作流
- 不做 399 个独立工具注册，改用检索式网关（接口仍然全量可达）
- 不保存用户百度账号密码，只保存 OAuth 令牌
- 不做竞价策略算法；出价建议只做接口返回的转述

## 八、许可

待定（Apache-2.0 或 MIT）。依赖的 Casdoor 为 Apache-2.0，MCP Python SDK 为 MIT。
