-- 百度订阅推送采样落库：替代 data/subscribe/*.jsonl（ADR 0009 D6）
--
-- 这个端点只做**采样**，不解析成业务动作（ADR 0003）。落库而非落盘的理由：
--   * 多副本下文件各写各的，样本会散落；库里能看到全量
--   * 与其它状态一致：运行期不再依赖本地磁盘

CREATE TABLE IF NOT EXISTS subscribe_events (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    received_at   timestamptz NOT NULL,
    method        text        NOT NULL DEFAULT '',
    path          text        NOT NULL DEFAULT '',
    client        text        NOT NULL DEFAULT '',
    key_source    text        NOT NULL DEFAULT '',
    query         jsonb,
    headers       jsonb,
    body          text        NOT NULL DEFAULT '',
    body_truncated boolean    NOT NULL DEFAULT false,
    body_json     jsonb
);

CREATE INDEX IF NOT EXISTS idx_subscribe_events_time
    ON subscribe_events (received_at DESC);
