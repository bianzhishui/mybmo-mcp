# 变更：调用审计落库 + 查询工具（audit-log-persistence）

> **状态**：✅ 已完成并上线（2026-11-12）。`MYBMO_AUDIT_BACKEND=pg` 正在线上运行，
> 文件后端保留可回滚。详见 `docs/audit.md`。

## 为什么

审计原先只写 `data/audit/*.jsonl`：

- **查不了**。要回答"昨天谁动过这个计划""这个账户最近哪些调用失败了"，只能 `grep` 文件
- **信息不足**。原记录只有 `capability + state + error`，没有入参/出参、没有 HTTP 状态码、
  没有起止时间，出了问题无法复现当时到底发了什么
- **跨租户无法按用户/账户切片**。文件按天分，不按维度索引

同时用户提出明确需求：**每通调用都要能看到入参、出参、开始/完成时间、谁调的、哪个账户、
HTTP 状态**，并且要有一个**带用户隔离的查询工具**。

## 变更内容

1. **建表** `audit_log`（`migrations/002_audit_log.sql`）：20 列，含
   `call_id / started_at / finished_at / duration_ms / tenant_id / user_sub / user_name /
   app / account_alias / account_name / capability / state / http_status / error_code /
   error / request_params(jsonb) / response_body(jsonb) / request_bytes / response_bytes /
   payload_digest`。
2. **抽象写入协议** `AuditSink`：`FileAuditSink`（原 `AuditLog`，JSONL）与 `PgAuditSink` 同接口，
   `execute_capability` 的调用点**一行未改**。
3. **传输层补齐事实**：新增 `Exchange` 快照（HTTP 状态码 + 原始响应原文），
   `BaiduApiError` 增加 `http_status`——否则审计拿不到"网关 502"和"业务 9015"的区别。
4. **新增工具** `query_audit`：按时间/账户/能力/状态/HTTP 码/入参包含匹配查询，**带用户隔离**。
5. **用户名兜底**：授权表 `display_name` 为空时回退令牌里的 `name`，避免"谁调的"是空白。
6. **配置**：`MYBMO_AUDIT_BACKEND=file|pg`、`MYBMO_AUDIT_STORE_PAYLOAD=true|false`。

## 不做

- **不清理、不分区**（按产品要求保留全部记录；体积风险已写入 `docs/audit.md`）
- **不截断**入参/出参（要求完整保存）
- 不做告警、不接 Grafana、不迁历史 JSONL（当时仅 16 条）

## 影响面

| 区域 | 变化 |
|---|---|
| `migrations/002_audit_log.sql` | 新增表与 5 个索引（含 `GIN request_params jsonb_path_ops`） |
| `audit/base.py` / `audit/log.py` / `audit/pg.py` | 审计契约 + 两个后端 |
| `baidu/transport.py` / `baidu/errors.py` | `Exchange` 快照、`http_status` |
| `tools/capability.py` | 记录新增字段（用户名/账户名/起止时间/http_status/出参） |
| `tools/audit.py` | 新增 `query_audit`（工具数 9 → 10） |
| `tools/runtime.py` | `audit` 改 `cached_property`，按后端选择实现 |
| `auth/tenancy.py` / `auth/pg_tenancy.py` | `resolve_principal(..., fallback_name=)` |
| `config.py` / `deploy/.env` | 两个新开关 |

## 验收标准（全部实测）

- [x] `audit_log` 建表幂等（临时 schema 连跑两次：`[1,2]` → `[]`）
- [x] 入参/出参**完整落库**（成功调用出参 `header+body` 全存，`response_bytes=447`）
- [x] 四态 `succeeded/failed/unknown/denied` 均可写入，且 `CHECK` 拦住非法值
- [x] `http_status` 正确：成功 200；请求未发出时 `NULL`
- [x] `user_name` / `account_name` 有值（`test01` / `baidu-YF电商-玛卡快消01`）
- [x] `call_id` 唯一索引生效，重复插入不抛异常（只记告警）
- [x] **写审计失败不影响调用**（PG 不可达时 `record()` 不抛、调用照常）
- [x] `query_audit` 隔离：test03 只看得到自己的记录，看不到 test01 的
- [x] 跨租户恒不可见（`tenant_id` 强制过滤）
- [x] 全量测试绿（310 passed / 21 skipped）
