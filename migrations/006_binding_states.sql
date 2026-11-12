-- 绑定 state 落库：替代 data/binding_state/<nonce>.json
--
-- 为什么必须落库（ADR 0009 D6）：
--   * 多实例部署时，实例 A 签发 state、回调可能打到实例 B——文件方案必然失败
--   * state 是一次性凭证的定位器，必须跨进程可见且能原子地"取用即删"
--
-- 一次性由 DELETE … RETURNING 保证：并发消费同一 nonce 时只有一个请求拿得到行。

CREATE TABLE IF NOT EXISTS binding_states (
    nonce      text        PRIMARY KEY,
    tenant_id  text        NOT NULL,
    subject    text        NOT NULL,
    alias      text        NOT NULL,
    app_id     text        NOT NULL DEFAULT '',
    issued_at  timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

-- 定期清理过期行（签发时顺手删，代价可忽略）
CREATE INDEX IF NOT EXISTS idx_binding_states_expires ON binding_states (expires_at);
