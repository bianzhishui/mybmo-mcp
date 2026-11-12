# ADR 0009：运行期只保留 PostgreSQL 后端（移除 file）

- 状态：已接受
- 日期：2026-11-12
- 决策者：项目负责人
- 关联：[ADR 0004](0004-multi-tenant-accounts.md)、[ADR 0007](0007-credentials-in-postgres.md)、
  [ADR 0008](0008-tenant-per-client.md)

## 背景

ADR 0007 落库时保留了 `file` 后端（`data/accounts/*.json`、`data/apps/*.json`、
`data/tenancy.json`、`data/audit/*.jsonl`），理由是"本地开发与单测不依赖 PG"。
实际代价是：同一件事两套实现要同步维护；测试一半跑文件、一半跑库，覆盖分叉；
配置项多一个就可能被误配（生产用 file 而无人察觉）。

多租户（ADR 0008）之后这种分裂更明显：租户维度只有 PG 能表达，file 后端只能退回单租户。

## 决策

### D1 运行期只保留 PostgreSQL

删除以下实现，`_build_context()` 直接构造 PG 组件：

| 删除 | 原用途 |
|---|---|
| `auth/vault.py`（`FileVault`） | `data/accounts/<alias>.json` |
| `auth/app_secrets.py` 的 `FileAppSecretStore` | `data/apps/<app_id>.json` |
| `auth/tenancy.py` 的 `TenancyStore` | `data/tenancy.json`（保留 `TenancyStore` 为 Protocol） |
| `audit/log.py`（`FileAuditSink`） | `data/audit/*.jsonl` |

`crypto.atomic_write()` 随之无人使用，一并删除（加解密仍由 `crypto.seal/open_json` 提供）。

### D2 测试用内存替身，**不放进 src**

`tests/support/memory.py` 提供 `MemoryCredentialStore` / `MemoryAppSecretStore` /
`MemoryTenancyStore` / `MemoryAuditSink`，外加 `attach_memory_context(runtime)` 把租户上下文
预填进 `Runtime`。

放测试目录而不是 `src/`：一旦进了 `src/` 它就变成"第三个后端"，迟早在生产被误用。
测试替身的审计记录复用 `audit.base.build_record()`，保证脱敏与 digest 语义和生产一致。

### D3 删掉三个后端开关

`MYBMO_TENANCY_BACKEND` / `MYBMO_AUDIT_BACKEND` / `MYBMO_CREDENTIAL_BACKEND` 全部移除；
少了凭证就去掉一整类"配错却没人发现"的故障。

### D4 一次性脚本删除

`scripts/migrate_credentials_to_pg.py`、`scripts/import_tenancy.py` 的使命已完成。
`scripts/build_capabilities.py` 保留——它写的是**源码文件**，属构建期工具。

### D5 `local` 认证模式下线（紧随其后）

`MODE_LOCAL` / `Principal.local_admin()` / `Principal.unrestricted` 一并删除。
代价：stdio 本地调试不再有隐式身份，调试改走 HTTP + JWT 或 CLI 子命令。

### D6 仍会写文件的三处，按批次处理

本 ADR 只删"存储后端"。运行期还有三处文件写，另有批次：

| 位置 | 现状 | 计划 |
|---|---|---|
| `auth/lock.py` | `data/locks/<alias>.lock`（**pg 后端也在用**） | 改 `SELECT … FOR UPDATE`（ADR 0007 D4 已实现 `locked()`） |
| `auth/binding.py` StateStore | `data/binding_state/<nonce>.json` | 落 `binding_states` 表 |
| `subscribe.append_record` | `data/subscribe/*.jsonl` | 落 `subscribe_events` 表 |

## 备选方案

| 方案 | 否决理由 |
|---|---|
| 保留 file 后端给测试 | 两套实现长期分叉；测试跑的不是生产路径 |
| 把内存实现放进 `src/` | 等于又造一个后端，必然被误用 |
| 单测全连 PG | 单测失去"零依赖"特性，本地与 CI 都要起库 |

## 后果

### 正面

- 只有一条代码路径，测试与生产同源
- 配置项少三个，误配面缩小
- 删掉约 500 行文件 I/O 代码

### 负面 / 需要承担

| 风险 | 应对 |
|---|---|
| 单测依赖内存替身，替身与 PG 行为可能漂移 | 替身复用 `build_record` 等共享逻辑；关键语义由 `test_pg_*` 覆盖 |
| 本地无 DB 时无法起服务 | 本地开发直接连开发库（PQ 很小）；`--help`/CLI 子命令仍可无库运行 |
| 一次性脚本删除后无法重放迁移 | 生产已迁移完成，且库有备份 |

## 未决问题

- [ ] `data/` 卷是否还有存在必要（等 D6 三处全部落库后摘掉）
- [ ] 内存替身与 PG 实现的一致性是否需要契约测试（同一组断言跑两边）
