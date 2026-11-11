# 设计：多租户落库

## 决策一览

| # | 决策 | 备选与否决原因 |
|---|---|---|
| D1 | **双后端**：`TenancyStore`（文件）/ `PgTenancyStore`（PG）同接口，用 `MYBMO_TENANCY_BACKEND` 切 | 直接替换成 PG：`local` 模式会被迫依赖数据库，且失去一键回滚 |
| D2 | 账户**私有**：`baidu_accounts.owner_user_id`；`account_grants` 只存 `read` / `write` | 账户归租户共享：与"绑定者私有"的产品语义冲突，且 admin 会被误以为可转授 |
| D3 | 迁移用 `NNN_*.sql` + `schema_version`，每个文件**单事务**、失败整体回滚 | 引 Alembic：为一个应用引整套迁移框架，收益不抵复杂度 |
| D4 | 导入用 upsert，并在导入后**自检 owner 完整性** | 一次性 INSERT：重跑会冲突，且丢 owner 时无人发现 |
| D5 | 切换前做**双读比对**（文件与 PG 对同一批身份给出相同结论） | 直接切：授权读错等于全员失权，代价远大于多花半小时 |
| D6 | 凭据**不落库**，仍走加密文件 | 同时改"存储位置"与"刷新语义"，出问题无法区分是哪边 |
| D7 | 审计仍写 JSONL | 本轮目标是把授权搬进库；审计跨租户查询等有需求再说 |

## 表结构要点

```
tenants(id, name, plan, status)
users(id, tenant_id, idp_subject, display_name, role)      UNIQUE(tenant_id, idp_subject)
baidu_accounts(id, tenant_id, alias, owner_user_id, ...)   UNIQUE(tenant_id, alias)
account_grants(user_id, account_id, permission)            CHECK(permission IN ('read','write'))
```

* `owner_user_id` 是 `NOT NULL` —— 没有归属的账户不该存在
* `account_grants` 的主键是 `(user_id, account_id)`，天然幂等
* 凭据相关三列（`secret_key_cipher` 等）**预留但恒为 NULL**

## 隔离怎么保证

1. 仓储层每条 SQL 都带 `tenant_id`（`tests/test_isolation.py` 静态扫描强制）
2. 写 grant 用 `INSERT ... SELECT ... WHERE a.tenant_id = %s`，即使传错 account_id 也写不出跨租户
3. `permission_for` 先按 `(tenant_id, alias)` 找账户，找不到直接 `None`（fail closed）

## 迁移与切换流程

```
① pg-provision.sh mybmo → DSN 写入 deploy/.env
② migrations/run.py（幂等）
③ scripts/import_tenancy.py --dry-run → 正式导入 → 自检 owner
④ 双读比对：test01=admin / test02=read / 陌生人=None，两侧一致
⑤ 设 MYBMO_TENANCY_BACKEND=pg 重启
⑥ 回滚：删掉该变量重启，回到文件版
```

## 影响面

| 区域 | 变化 |
|---|---|
| `auth/tenancy.py` | 保持文件实现，新增 `stats()` 与 mtime 重载 |
| `auth/pg_tenancy.py` | 新增 PG 实现（同接口） |
| `config.py` | 新增 `MYBMO_TENANCY_BACKEND` |
| `migrations/` | 新增 `001_init.sql` 与 `run.py` |
| `deploy/Dockerfile` | `uv sync --extra db` 装 psycopg；`migrations/` 打进镜像 |

## 未做（明确记录）

* **MCC 子账户**：字段已留，逻辑等真实子账户再补
* 凭据落库、审计落库、连接池（当前每次调用新建连接，量小可接受）
