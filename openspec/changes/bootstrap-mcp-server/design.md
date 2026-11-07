## Context

全新项目。目标是把百度营销开放平台封装成 MCP 服务，并在公网安全地服务多账户、多用户。
鉴权的整体选型已由 [ADR 0001](../../../docs/decisions/0001-auth-architecture.md) 确定：
**自部署 Casdoor 作授权服务器，本服务作 Resource Server**。

本文只记录该变更内部的技术取舍。

> **2026-11-07 方向调整**：D1（不用 SDK）、D2（按业务闭环划工具粒度）、D3（写操作审批）
> 已被 [ADR 0003](../../../docs/decisions/0003-capability-only.md) 取代。
> 下文保留原始判断，供回溯当时为什么这么想。

## Goals / Non-Goals

**Goals**

- 用 10 个左右的工具覆盖"看数据、拓词、改投放"三条主线
- 多账户互不串号，令牌按账户隔离且可刷新
- 写操作必须有不可绕过的人工确认环节
- 无凭据、无网络时给出明确可执行的回应，而不是空转或假数据

**Non-Goals**

- 不做通用 API 网关：写接口不开放透传
- 不做竞价算法：出价只转述接口建议值
- 不做前端：管理动作走 CLI 与 MCP 工具
- 不追求 100% 接口覆盖：长尾走只读透传

## Decisions

### D1. 传输层用原生 HTTP，不把 `baiduads-sdk` 作为硬依赖

`baiduads-sdk` 最后发布是 2023-05，之后无更新；接口目录也已落后于线上。
它的价值在**模型定义与字段名参考**，不在运行时。

- 运行时：自建轻量传输层，直接 POST 到 `api.baidu.com/json/{sms|feed}/service/{Service}/{Method}`
- 参考：需要字段语义时查 SDK 生成的 model
- 验证：P1 阶段做一次 SDK 在 Python 3.12 上的导入 POC，结论写入 `docs/`，但不进依赖树

替代方案：直接用 SDK。否决理由是停更 + 传递依赖（旧 urllib3）风险 + 无法覆盖新接口。

### D2. 工具粒度按"业务闭环"而非"接口"

一个工具背后可能调多个接口（例如 `keyword_plan` = 拓词 + 出价建议 + 竞争度）。
理由：模型选工具的准确率随工具数量下降；399 个工具在实践中不可用。

写操作固定两个工具：`change_preview` 与 `change_execute`。
替代方案：按业务动作拆成 `add_keywords`、`update_bid` 等。否决原因是每个写动作都要重复实现审批，
且新增动作就要新增工具。

### D3. 审批用"内容哈希 + 一次性 ID"，不用布尔确认

```text
approval_id = random(16)
记录: (approval_id, user_sub, tenant_id, account_id, payload_hash, scope, expires_at, used_at)
```

执行时四项全等才放行：哈希一致、未使用、未过期、账户归属正确。

替代方案：`confirm=true` 参数。否决理由——模型可以自己传 `true`，等于没有确认。

### D4. 百度 OAuth 的 state 用一次性 nonce

`state` 必须满足：不可推导、一次性、有 TTL、绑定 `sub + tenant + purpose`。
不使用 `md5(appId_uid)` 这类可复现值（现有 skill 的做法，存在回调抢占风险）。

已知不确定点：百度是否原样回传自定义 `state` **尚未实证**。
因此回调处理必须能退化：若 `state` 不匹配，按 `userId` 命名账户并**标记该账户为待确认**，
而不是直接失败或直接信任。

### D5. 凭据分层存储

| 环境 | 方案 |
|---|---|
| 本地开发 / stdio | AES-GCM 加密文件 + 权限 600，密钥来自 `MYBMO_MASTER_KEY` |
| 单机部署 | 同上；接入 PG 后改存 `bytea` 列 |
| 多租户生产 | KMS 信封加密，每租户独立 DEK |

不使用系统 keyring：无头 Linux 需 SecretService 守护进程，macOS 会弹授权框，CI 中常不可用。
统一 AES-GCM（`cryptography`）只有一条代码路径、一套测试。详见 [ADR 0002](../../../docs/decisions/0002-tech-stack.md) 的 D7。

### D6. 多租户先用"单 Casdoor 组织 + 业务侧授权表"

Casdoor 的 Organization 与业务 `tenant` 是否一一对应，暂不锁定。
P4 先用单组织跑通，租户隔离靠 `tenants` + `account_grants` 两张表保证；
若后续需要客户自助管理成员，再启用 Casdoor Organizations 映射。

理由：过早把两套多租户模型对齐，会产生大量无收益的映射代码。

### D7. 读写重试策略分离

| 类型 | 重试 | 理由 |
|---|---|---|
| 读 | 最多 2 次，指数退避 | 幂等，重试安全 |
| 写 | **0 次** | 超时属"结果未知"，重发可能重复创建 |

写失败后返回 `state=unknown` 并提示先查询，不自动补偿。

## Risks / Trade-offs

| 风险 | 影响 | 缓解 |
|---|---|---|
| 百度 `state` 不回传 | 多账户别名失效 | 退化为 `acct_<userId>`，并在响应中明示 |
| 写接口字段未经真实账户验证 | 真实执行可能报参数错误 | 先只读跑通再开写；写工具标 `experimental` |
| Casdoor DCR 被滥用 | 垃圾应用堆积 | 回调白名单 + 注册限流 + 定期清理 |
| 单机部署无 HA | 认证或服务宕机即不可用 | 明确标注为单点；后续再谈多副本 |
| 报表接口分页/上限不确定 | 数据可能不完整 | 响应里带 `truncated` 与 `total`，不假装全量 |

## Migration Plan

全新项目，无迁移。上线顺序：

1. 本地 stdio 跑通只读，不接 Casdoor（用环境变量里的测试身份）
2. 加 Casdoor，stdio 换成 Streamable HTTP
3. 加写操作审批
4. 公网部署，管理台不暴露

## Open Questions

- [ ] 百度报表接口的单次返回上限与分页参数，需要真实账户实测
- [ ] 关键词/创意的"暂停"字段名，需以官方 schema 或脱敏响应核实后才能实现默认暂停
- [ ] Casdoor 的 `dcrPolicy` 能否限制注册来源
- [ ] 是否需要 `api_read` 之外的写透传（当前判断：不需要）
