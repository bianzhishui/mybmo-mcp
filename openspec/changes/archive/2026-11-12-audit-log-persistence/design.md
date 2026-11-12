# 设计：调用审计落库

## 决策一览

| # | 决策 | 备选与否决原因 |
|---|---|---|
| D1 | **不建外键**（`user_sub` / `account_alias` / `user_name` / `account_name` 冗余存文本） | 指向 `users.id` / `baidu_accounts.id`：用户未登记、账户删除时**写不进去**，而审计丢失比失去引用完整性严重得多 |
| D2 | **入参/出参完整保存，不截断** | 截断（如 64KB）省空间，但"回放当时到底发了什么"是这次的核心诉求，截断就失去意义 |
| D3 | **审计是旁路，失败不阻断调用** | 与业务同事务：PG 抖动会连带业务失败，代价远大于丢一条日志 |
| D4 | **单连接 + 线程锁**，超时 3s/2s | 上连接池：审计量小（当时 16 条/天），池的复杂度不值得 |
| D5 | 保留**四态** `succeeded/failed/unknown/denied` | 只留成功/失败：`unknown`（超时/5xx，不知道远端做没做）是写操作排查的唯一凭据；`denied` 是越权线索 |
| D6 | 传输层加 **`Exchange` 快照** | 只记 `state`：拿不到 HTTP 状态码和出参原文，分不清"网关挂了"和"参数不对" |
| D7 | `query_audit` 的**可见范围在工具层算**，SQL 只收结果 | 把权限判断写进 SQL：权限逻辑会散到查询语句里，难审计 |
| D8 | 用户隔离规则：自己 + 自己拥有账户下的全部 | 只给自己：owner 无法排查"谁动了我的计划"；给同租户全部：越权 |
| D9 | `params_match` 用 `jsonb @> ` + `GIN (jsonb_path_ops)` | 全文 `ILIKE`：无索引全表扫，且语义模糊 |

## 表结构要点

```
audit_log(id, call_id UNIQUE, started_at, finished_at, duration_ms,
          tenant_id, user_sub, user_name, app, account_alias, account_name,
          capability, state CHECK(...), http_status, error_code, error,
          request_params jsonb, response_body jsonb,
          request_bytes, response_bytes, payload_digest)
索引：(tenant_id, started_at DESC) / (user_sub, started_at DESC)
      (account_alias, started_at DESC) / (capability, started_at DESC)
      部分索引 (started_at DESC) WHERE state <> 'succeeded'
      GIN (request_params jsonb_path_ops)
```

## 写入路径

```
execute_capability → runtime.audit.record(...) → FileAuditSink | PgAuditSink
```

`AuditSink` 协议让调用点零改动；`build_record()` 在 `audit/base.py` 里统一字段映射，
两种后端不会各写一份（避免字段漂移）。

## 隔离怎么保证

1. 工具层算出 `visible_subs` / `visible_aliases`，交给 SQL；
   `unrestricted` 才传 `None`（= 不限）
2. 仓储层每条 SQL 都带 `tenant_id`，条件片段由固定字符串拼、取值全走 `%s`
3. 显式传别人的 `user` → `AccessDenied`；无权限的 `account` → `AccessDenied`
4. 两者都没有可用范围时拼 `false`，**结果是空集而不是全量**

## 影响性能的地方

| 项 | 影响 |
|---|---|
| 大 jsonb 写入 | 单条约 10–50ms（MB 级响应更久），但只在审计链路，不阻塞业务响应 |
| `params_match` 查询 | 有 GIN 索引，正常；无索引时全表扫 |
| 表增长 | 不清理不分区，按需求接受；`request_bytes/response_bytes` 用于发现异常大记录 |

## 未做（明确记录）

* 清理/保留期任务（`prune_audit.py`）—— 按需求"不清理"，将来需要再补
* 历史 JSONL 导入（当时 16 条，收益极低）
* 生产级"停库演练"—— `MYBMO_DATABASE_URL` 同时供授权表使用，停库会先影响鉴权，
  无法单独验证审计降级；降级行为由单测 `test_unreachable_database_does_not_raise` 覆盖
