# 任务：调用审计落库 + 查询工具

> 勾选标准：能被 SQL 查询、测试或线上实测证明。

## 1. 传输层事实补齐

- [x] 1.1 `Exchange` 快照（url / http_status / response_text / 起止时间 / duration_ms）
- [x] 1.2 成功、4xx、5xx、超时四条路径都写快照；未发出的请求保持 `None`
- [x] 1.3 `BaiduApiError` 增加 `http_status`（业务错误码也算上）
- [x] 1.4 `resolve_principal` 支持 `fallback_name`（令牌 `name` 兜底 display_name）

**出口**：`tests/test_transport.py` 新增 5 例覆盖上述路径。

## 2. 建表与迁移

- [x] 2.1 `migrations/002_audit_log.sql`（20 列 + 5 个索引 + 四态 CHECK）
- [x] 2.2 临时 schema 里连跑两次：`[1,2]` → `[]`（幂等）
- [x] 2.3 断言表存在、`schema_version` 有 1 与 2

**出口**：`tests/test_migrations.py` 覆盖；线上 `已应用版本: [2]`。

## 3. 写入协议与文件后端

- [x] 3.1 `audit/base.py`：`AuditRecord` / `AuditSink` 协议 / 四态常量 / `build_record`
- [x] 3.2 `AuditLog` → `FileAuditSink`（保留 `AuditLog` 别名，导入不破）
- [x] 3.3 `MYBMO_AUDIT_STORE_PAYLOAD` 开关：关掉只留摘要

**出口**：既有 295 测试全绿，行为不变。

## 4. PG 后端与接线

- [x] 4.1 `PgAuditSink.record()`：jsonb 写入、脱敏、单连接复用、异常只告警
- [x] 4.2 `Runtime.audit` 改 `cached_property`，按 `MYBMO_AUDIT_BACKEND` 选择
- [x] 4.3 集成测试：四态、字段完整性、入参/出参深比较还原、重复 `call_id` 不抛、
      禁用 payload 时不写正文、库不可达不抛异常

**出口**：`tests/test_pg_audit.py` 13 例通过（真实 PostgreSQL + 临时 schema）。

## 5. 查询工具

- [x] 5.1 `query_audit`：时间区间 / 账户 / 能力前缀 / state / http_status / error_code /
      call_id / user / params_match / limit≤200 / offset / include_payload
- [x] 5.2 隔离：普通用户只见自己；owner 可看自己账户下全部；`unrestricted` 全见
- [x] 5.3 越权用例：传别人 `user`、访问无权限 `account` 均 `AccessDenied`
- [x] 5.4 文件后端下给明确提示（引导去看 JSONL）

**出口**：`tests/test_query_audit.py` 9 例；线上 test03 查询只见自己 2 条。

## 6. 上线与文档

- [x] 6.1 线上执行 `migrations/run.py`（`已应用版本: [2]`）
- [x] 6.2 `deploy/.env` 设 `MYBMO_AUDIT_BACKEND=pg`、`MYBMO_AUDIT_STORE_PAYLOAD=true`
- [x] 6.3 实测成功调用：`succeeded / 200 / test01 / baidu-YF电商-玛卡快消01 / response_bytes=447`
- [x] 6.4 实测失败调用：`failed / NULL / test03`（无可用账户，请求未发出）
- [x] 6.5 `docs/audit.md` 新增；`architecture.md` / `auth-contract.md` / README / project 同步
- [x] 6.6 回滚路径验证：删 `MYBMO_AUDIT_BACKEND` 即回文件后端（与 tenancy 同款机制）

## 7. 上线后修的三个自引入缺陷

- [x] 7.1 `runtime.audit` 双重 `@cached_property` → 取属性抛 `TypeError`（加回归测试）
- [x] 7.2 `query()` 对已是 dict 的行重复 `dict()` → 报错
- [x] 7.3 生产连接无 `row_factory` 返回元组 → `dict(tuple)` 崩；改为按游标列名 zip
      （补"无 row_factory"回归测试）

**出口**：310 passed / 21 skipped；容器代码与 HEAD 34 文件逐一比对一致。
