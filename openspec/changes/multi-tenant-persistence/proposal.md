# 变更：多租户落库（multi-tenant-persistence）

> **触发条件**：出现第二个真实用户，或需要按租户出审计报表。当前不启动。

## 为什么

授权现在是文件式 `data/tenancy.json`，只能表达单租户：

- 没有 `tenant_id` 维度，跨租户隔离靠"部署多份"而不是数据隔离
- 授权关系无法按租户查询、无法并发安全地修改（整文件重写）
- `users` 与 IdP 的 `sub` 映射没有持久化，换机器就丢
- `audit_log` 的租户维度只能事后从文件名猜

`psycopg` 已在 `optional-dependencies.db` 里备好，库与角色由 infra 创建，缺的是表结构与仓储层。

## 变更内容

1. **建三表**：`tenants` / `users` / `account_grants`（结构见 `docs/architecture.md` 第四节）。
   `baidu_accounts` 与 `audit_log` 暂不落库——账户凭据仍在加密文件里，审计仍是 JSONL。
2. **迁移机制**：`migrations/NNN_*.sql` + `schema_version` 表 + 约 40 行 runner（ADR 0002 D4）。
3. **仓储层**：新增 `PgTenancyStore`，与现有 `TenancyStore` 同接口；
   `resolve_principal(sub)` 与 `grants_for(principal)` 的语义不变，调用方零改动。
4. **一次性数据迁移**：把 `tenancy.json` 的账户别名与档位导入 `account_grants`。
5. **隔离约束**：所有查询强制带 `tenant_id`，由仓储层封装，不散落在业务代码的 `if` 里。
6. **MCC 子账户**：`kind=mcc_child` 沿 `parent_account_id` 取父账户令牌（ADR 0004 D2）。

## 不做

- 不迁 `baidu_accounts`：凭据加密与轮换逻辑不动，避免同时改"存储位置"与"刷新语义"
- 不改审计：仍是按天 JSONL，落库留到需要跨租户查询时再说
- 不引 ORM、不引连接池中间件（进程内 `psycopg_pool` 即可）

## 影响面

| 区域 | 变化 |
|---|---|
| `migrations/` | 新增目录与三个 SQL |
| `auth/tenancy.py` | 抽出接口，新增 PG 实现；文件实现保留给 `local` |
| `config.py` | `MYBMO_DATABASE_URL` 从"忽略"变为"oidc 模式必填" |
| `deploy/Dockerfile` | 两条 `uv sync` 追加 `--extra db` |
| `tests/` | 仓储层单测（用临时 schema），保持离线可跑 |

## 验收标准

- [ ] 空库执行迁移 → 三表与 `schema_version` 就位，重复执行幂等
- [ ] `local` 模式仍可只用文件，不要求数据库
- [ ] `oidc` 模式从库读授权：跨租户账户不可见，越权按 grant 拒绝
- [ ] `tenancy.json` 的数据能一次性导入且不丢档位
- [ ] 子账户调用时 `userName` 取子账户名（不是父账户）
- [ ] 既有测试全绿；新增仓储层测试覆盖默认拒绝与档位顺序
