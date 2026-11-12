-- 调用日志落库：记录每一次 execute_capability 的真实请求与响应
--
-- 设计要点：
--   * **不建外键**：用户未登记、账户被删时审计仍要写得进去；
--     user_sub / account_alias / user_name / account_name 冗余存文本
--   * **不截断**：入参、出参完整保存（业务要求能回放当时到底发了什么）
--   * `state` 四态：succeeded / failed / unknown / denied，
--     其中 unknown = 不知道远端做没做（超时、5xx），是写操作排查的唯一凭据
--   * `payload_digest`：不读正文就能比对"两次调用是不是同一报文"

CREATE TABLE IF NOT EXISTS audit_log (
    id             bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    call_id        text        NOT NULL,
    started_at     timestamptz NOT NULL,
    finished_at    timestamptz,
    duration_ms    integer     NOT NULL DEFAULT 0,
    tenant_id      text        NOT NULL,
    user_sub       text        NOT NULL,
    user_name      text        NOT NULL DEFAULT '',
    app            text        NOT NULL DEFAULT '',
    account_alias  text        NOT NULL DEFAULT '',
    account_name   text        NOT NULL DEFAULT '',
    capability     text        NOT NULL,
    state          text        NOT NULL
                   CHECK (state IN ('succeeded', 'failed', 'unknown', 'denied')),
    http_status    integer,
    error_code     integer,
    error          text        NOT NULL DEFAULT '',
    request_params jsonb,
    response_body  jsonb,
    request_bytes  integer     NOT NULL DEFAULT 0,
    response_bytes integer     NOT NULL DEFAULT 0,
    payload_digest text        NOT NULL DEFAULT ''
);

-- call_id 每次调用唯一，用来把"返回给调用方的编号"反查回记录
CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_call_id ON audit_log (call_id);

CREATE INDEX IF NOT EXISTS idx_audit_tenant_time  ON audit_log (tenant_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_user_time    ON audit_log (user_sub, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_account_time ON audit_log (account_alias, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_cap_time     ON audit_log (capability, started_at DESC);

-- problem 类记录（失败/未知）单独一条部分索引：排查时几乎只查这些
CREATE INDEX IF NOT EXISTS idx_audit_problem ON audit_log (started_at DESC)
    WHERE state <> 'succeeded';

-- 入参包含匹配（查询工具按请求参数搜）走 jsonb_path_ops，比默认 GIN 小且够用
CREATE INDEX IF NOT EXISTS idx_audit_params ON audit_log USING GIN (request_params jsonb_path_ops);
