# 调用审计（audit_log）

记录**每一次 `execute_capability`**：谁、在什么时候、用哪个百度账户、调了哪个接口、
发了什么、收到什么、结果如何。默认写文件，可切到 PostgreSQL 查询。

## 一、两种后端

| 后端 | 开关 | 存储 | 适用 |
|---|---|---|---|
| 文件（默认） | `MYBMO_AUDIT_BACKEND=file` | `/data/audit/YYYY-MM-DD.jsonl`，一行一条 | 单机、只想留痕 |
| 数据库 | `MYBMO_AUDIT_BACKEND=pg` | `audit_log` 表（`migrations/002_audit_log.sql`） | 需要按人/账户/接口检索 |

两者字段契约完全一致（`src/mybmo_mcp/audit/base.py`），切换只改一个环境变量。

## 二、字段说明

| 字段 | 含义 | 备注 |
|---|---|---|
| `id` | 自增主键 | 仅供排序分页 |
| `call_id` | 本地调用编号 | 与返回给调用方的 `meta.call_id` 一致，可反查 |
| `started_at` / `finished_at` / `duration_ms` | 开始 / 结束 / 耗时 | `finished_at` 含异步任务轮询时间 |
| `tenant_id` | 租户 | 所有查询强制带它 |
| `user_sub` | 调用者 IdP 身份 | Casdoor 的 `sub` |
| `user_name` | 调用者名称 | 授权表 `display_name`，为空时回退令牌里的 `name` |
| `app` | 令牌的 `azp` / client_id | 区分同一个人用的不同客户端 |
| `account_alias` | 账户别名（`main`） | 程序用 |
| `account_name` | 百度账户中文名 | 给人看，如 `baidu-YF电商-玛卡快消01` |
| `capability` | 能力键 | `服务.方法` |
| `state` | `succeeded` / `failed` / `unknown` / `denied` | `unknown` = 不知道远端做没做（超时、5xx），写操作排查的关键 |
| `http_status` | HTTP 状态码 | 区分"网关挂了"和"业务报错"；请求没发出去时为 `NULL` |
| `error_code` | 百度业务错误码 | 如 `9015` |
| `error` | 错误信息 | 已过 `redact()`，令牌片段变 `***` |
| `request_params` | **入参**（`execute_capability` 的 `input`） | **完整保存**，不含 accessToken |
| `response_body` | **出参**（百度响应完整 JSON） | **完整保存**；解析失败时存 `{"_raw": "..."}` |
| `request_bytes` / `response_bytes` | 报文原始字节数 | 便于发现异常大的调用 |
| `payload_digest` | 入参 sha256 前 16 位 | 不读正文就能比对"两次是不是同一报文" |

**不存令牌**：`build_payload` 加的 `header.accessToken` 永不入库。

## 三、体积与增长（重要）

按需求**不截断、不清理**，所以：

* 单条记录可能到 **MB 级**（如 `getKeywordPackages(pageSize=1000)`、报表任务）
* 表会持续增长，目前量级很小（2026-11-11 全天 16 条），但大响应调用会放大
* 观察手段：`request_bytes` / `response_bytes` + 下面的 SQL

```sql
-- 最近 7 天最"重"的 10 次调用
SELECT started_at, user_name, account_name, capability, response_bytes
FROM audit_log
WHERE started_at > now() - interval '7 days'
ORDER BY response_bytes DESC LIMIT 10;
```

需要收紧时有两个开关（无需改代码）：

* `MYBMO_AUDIT_STORE_PAYLOAD=false` —— 只存摘要，不存正文
* 直接对上表加清理任务（当前**未提供**自动清理脚本，按现状设计不清理）

## 四、写入是旁路

```
execute_capability → runtime.audit.record(...) → FileAuditSink / PgAuditSink
```

* **审计写失败不影响调用**：PG 连接超时 3s、语句超时 2s，失败只打
  `AUDIT WRITE FAILED` 到 stderr，业务照常返回
* 审计**不在业务事务里**，也不会因为审计失败回滚百度侧的写操作
* 单连接 + 线程锁复用（审计量小，不值得上连接池）

## 五、查询工具 `query_audit`

只在 `MYBMO_AUDIT_BACKEND=pg` 时可用；文件后端会提示直接看 JSONL。

```jsonc
// 我最近失败的写操作
{"started_after": "2026-11-12T00:00:00+08:00", "state": "failed", "limit": 20}

// 某个账户下所有人的调用（仅 owner）
{"account": "main", "started_after": "2026-11-11T00:00:00+08:00"}

// 按入参内容找：谁调过这个计划
{"params_match": {"campaignShoppingIds": [2079251858]}}

// 要原文
{"call_id": "c098c3ee7852", "include_payload": true}
```

参数：`started_after` / `started_before` / `account` / `capability`（前缀匹配）/
`state` / `http_status` / `error_code` / `call_id` / `user` / `params_match` /
`limit`(≤200) / `offset` / `include_payload`(默认 false)。

### 隔离规则

| 调用者 | 能看到 |
|---|---|
| 普通用户 | **只有 `user_sub = 自己`** |
| 账户 owner | 自己的 + **该账户下所有人的调用** |

* 所有查询强制带 `tenant_id`，跨租户永远查不到
* 显式传别人的 `user` → `AccessDenied`（不做"悄悄降级成自己"）
* 访问无权限的 `account` → `AccessDenied`

## 六、运维

```bash
# 建表（幂等，可重复执行）
docker exec -e MYBMO_DATABASE_URL="$DSN" mybmo-mcp-1 python /app/migrations/run.py

# 切到数据库后端
# deploy/.env 里加：
#   MYBMO_AUDIT_BACKEND=pg
#   MYBMO_AUDIT_STORE_PAYLOAD=true
docker compose up -d --force-recreate mcp

# 回滚：删掉 MYBMO_AUDIT_BACKEND（或设回 file）重启即可
```

常用 SQL：

```sql
-- 谁在什么时候动了哪个账户
SELECT started_at, user_name, account_name, capability, state, error_code
FROM audit_log WHERE state <> 'succeeded' ORDER BY started_at DESC LIMIT 50;

-- 某次调用的原始请求/响应
SELECT request_params, response_body FROM audit_log WHERE call_id = 'c098c3ee7852';
```

## 七、当前线上状态（2026-11-12）

```
MYBMO_AUDIT_BACKEND=pg        audit_log 表已建（migrations 002）
MYBMO_AUDIT_STORE_PAYLOAD=true 入参/出参完整入库
```

实测（生产库）：

| 场景 | state | http_status | user_name | account_name | 备注 |
|---|---|---|---|---|---|
| test01 读计划成功 | `succeeded` | `200` | `test01` | `baidu-YF电商-玛卡快消01` | 出参含 header+body，`request_bytes/response_bytes` 有值 |
| test03 无可用账户 | `failed` | `NULL` | `test03` | 空 | 请求没发出去，所以没有 account_name / http_status |

隔离实测：test03 调 `query_audit` 只看到自己的 2 条，**看不到** test01 那条成功记录。

## 八、已知限制

1. **不清理、不分区**：按需求保留全部记录，长期需要人工关注体积
2. **`params_match` 是 jsonb 包含匹配**：已建 `GIN (request_params jsonb_path_ops)` 索引
3. **`unknown` 与 `denied` 的区分**看 `state`；早期只用文件的记录没有 `http_status`
4. 出参存的是百度原始响应（含 `header`/`body`），不是裁剪后的 `data`
