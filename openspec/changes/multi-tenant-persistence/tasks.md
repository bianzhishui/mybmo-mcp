# 任务：多租户落库

> 勾选标准：能被 SQL 查询或测试证明。
> 本变更的**前置条件是出现第二个真实用户**；在此之前不做，避免为空需求背复杂度。

## 1. 准备

- [x] 1.1 向 infra 申请库与角色：`pg-provision.sh <app>`，拿 DSN
- [x] 1.2 把 `MYBMO_DATABASE_URL` 填进 `deploy/.env`，确认连接可用
- [x] 1.3 `pyproject.toml` 的 `db` extra 装进镜像（Dockerfile 两条 `uv sync` 加 `--extra db`）

**出口**：容器内能连上库，`select 1` 成功。

## 2. 迁移机制

- [x] 2.1 `migrations/001_init.sql`：`tenants` / `users` / `account_grants` + `schema_version`
- [x] 2.2 `migrations/run.py`：按文件名顺序执行未应用的迁移，单事务
- [x] 2.3 重复执行幂等；失败时不留半套表
- [ ] 2.4 单测：用临时 schema 跑一次迁移，断言表与版本记录

**出口**：空库跑一次得到完整结构；再跑一次无事发生。

## 3. 仓储层

- [x] 3.1 抽出 `TenancyStore` 协议：`resolve_principal(sub)` / `grants_for(principal)` / `grant(...)`
      以及账户归属三件套 `owner_of(alias)` / `register_account(...)` / `share(...)`（账户私有模型）
- [x] 3.2 现有文件实现改名为 `FileTenancyStore`，只用于 `local`
- [x] 3.3 新增 `PgTenancyStore`：所有查询带 `tenant_id`，不暴露裸 SQL 给上层
- [x] 3.4 `Runtime.tenancy` 按 `MYBMO_AUTH_MODE` 选择实现
- [x] 3.5 单测：默认拒绝、档位顺序（`read < write < admin`）、跨租户不可见、`sub` 不存在时拒绝

**出口**：`oidc` 模式下越权调用在取凭据前被拒；`local` 模式不依赖数据库。

## 4. 数据迁移

- [ ] 4.1 `scripts/import_tenancy.py`：读 `tenancy.json`，写 `tenants` / `users` / `account_grants`
- [ ] 4.2 导入前校验：别名在租户内唯一、档位合法、`sub` 不重复
- [ ] 4.3 导入后比对：文件里的条数 == 库里条数；抽样比对档位
- [ ] 4.4 保留原文件直到验证通过（不删）

**出口**：导入后行为与文件版一致，回滚只需把 `MYBMO_AUTH_MODE` 指回文件实现。

## 5. 隔离约束

- [x] 5.1 审计日志与工具调用的 `tenant_id` 来源统一为 `Principal`
- [x] 5.2 不许业务代码直接拼 `tenant_id` 条件：只经仓储层
- [ ] 5.3 加一条静态检查或测试：`SELECT` 语句必须出现 `tenant_id`

**出口**：代码里找不到"忘了带租户条件"的查询。

## 6. MCC 子账户

- [ ] 6.1 账户模型支持 `kind=mcc_child` 与 `parent_account_id`
- [ ] 6.2 取凭据时上溯一层父账户令牌，但 `userName` 用子账户名
- [ ] 6.3 `list_accounts` 把父子关系显式暴露，避免模型选错
- [ ] 6.4 `[live]` 用一个真实 MCC 子账户验证：调 `getKRByQuery` 不报 81020

**出口**：子账户能独立调用，且不会误用父账户身份。

## 7. 切换与回滚

- [ ] 7.1 先在预发库跑通迁移 + 导入 + 一次读调用
- [ ] 7.2 生产切换：改 `MYBMO_DATABASE_URL` 与模式，重建容器
- [ ] 7.3 回滚预案：保留文件实现与 `tenancy.json`，切回即恢复
- [ ] 7.4 全量测试 + `ruff` 绿

**出口**：切换窗口内可 5 分钟回滚。

## 进展记录（2026-11-11）

已完成并线上验证：

- 库 `mybmo` 建好，DSN 在 `deploy/.env`（`MYBMO_DATABASE_URL`）
- `migrations/001_init.sql` 应用成功（5 张表），重复执行幂等
- `PgTenancyStore` 与文件版**双读一致**：test01 owner=admin、test02 grant=read、陌生人 None
- `tenancy.json` 导入 PG：1 账户 / 1 owner / 1 grant，owner 完整性自检通过
- 切换 `MYBMO_TENANCY_BACKEND=pg` 后：`/healthz` users 1→2、grants=1；
  test02 读放行、写被拒（AccessDenied）
- 回滚方式：删掉该环境变量并重启，即回文件版

补充完成（2026-11-11）：

- 3.5 PG 仓储层单测：`tests/test_pg_tenancy.py` 7 例（临时 schema，跑完即删）
  覆盖 owner=admin、share/revoke、admin 不可转授、陌生人拒绝、档位不足、跨租户隔离、describe 形状
- 第 5 节隔离约束：`tests/test_isolation.py` 静态扫描 pg_tenancy.py，
  断言每条 SELECT 都带 tenant_id、每条 INSERT/UPDATE/DELETE 都落在本租户；
  顺带把 grant 写入改成 `INSERT ... SELECT ... WHERE a.tenant_id = %s`（更严）
- 跑法：`MYBMO_TEST_DATABASE_URL=postgresql://... pytest tests/test_pg_tenancy.py`

未完成：第 6 节 MCC 子账户（需真实子账户）。
