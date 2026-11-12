-- 008：统一表规范（ADR 0010）
--
-- 规范：
--   1. 每张业务表第一列是 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY
--   2. 最后两列固定是 created_at / updated_at（timestamptz NOT NULL DEFAULT now()）
--   3. updated_at 由触发器 set_updated_at() 自动维护
--   4. 业务自然键保留为 UNIQUE（ON CONFLICT 依赖唯一约束，不依赖主键）
--   5. schema_version 豁免：它是迁移账本，version 即主键
--
-- 为什么重建而不是逐列 ALTER：
--   * Postgres 不能调整列顺序，`id` 要排第一只能重建
--   * tenants 被 users / baidu_accounts 外键引用，改它的列序绕不开重建
--   * 当前数据量 < 20 行，重建成本可忽略；单事务执行，失败整体回滚
--
-- 代理键 vs 自然键：
--   数字 id 只做代理主键，**业务层继续用文本自然键**（tenants.tenant_key =
--   原来的 "t_default"），所以 tenant_id 列保持 text 并外键指向 tenant_key。
--   这样日志、审计、CLI 里看到的仍是可读的租户标识，应用代码也无需改传参。

-- ---------------------------------------------------------------- 基础设施

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 所有时间按 Asia/Shanghai 显示（timestamptz 存 UTC，显示取决于会话时区）
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I SET timezone = %L', current_database(), 'Asia/Shanghai');
END $$;

-- ---------------------------------------------------------------- 备份

CREATE TABLE _bak_tenants           AS SELECT * FROM tenants;
CREATE TABLE _bak_users             AS SELECT * FROM users;
CREATE TABLE _bak_baidu_accounts    AS SELECT * FROM baidu_accounts;
CREATE TABLE _bak_account_grants    AS SELECT * FROM account_grants;
CREATE TABLE _bak_credentials       AS SELECT * FROM credentials;
CREATE TABLE _bak_app_secrets       AS SELECT * FROM app_secrets;
CREATE TABLE _bak_binding_states    AS SELECT * FROM binding_states;
CREATE TABLE _bak_audit_log         AS SELECT * FROM audit_log;
CREATE TABLE _bak_subscribe_events  AS SELECT * FROM subscribe_events;

-- ---------------------------------------------------------------- 拆除（先子后父）

DROP TABLE account_grants;
DROP TABLE baidu_accounts;
DROP TABLE users;
DROP TABLE credentials;
DROP TABLE app_secrets;
DROP TABLE binding_states;
DROP TABLE audit_log;
DROP TABLE subscribe_events;
DROP TABLE tenants;

-- ---------------------------------------------------------------- 按规范重建

CREATE TABLE tenants (
    id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_key text        NOT NULL UNIQUE,
    name       text        NOT NULL,
    plan       text        NOT NULL DEFAULT 'free',
    status     text        NOT NULL DEFAULT 'active',
    client_id  text        UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id    text        NOT NULL REFERENCES tenants (tenant_key) ON DELETE CASCADE,
    idp_subject  text        NOT NULL,
    email        text        NOT NULL DEFAULT '',
    display_name text        NOT NULL DEFAULT '',
    status       text        NOT NULL DEFAULT 'active',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idp_subject)
);

CREATE TABLE baidu_accounts (
    id                      bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id               text        NOT NULL REFERENCES tenants (tenant_key) ON DELETE CASCADE,
    alias                   text        NOT NULL,
    owner_user_id           bigint      NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    kind                    text        NOT NULL DEFAULT 'standalone'
                                        CHECK (kind IN ('standalone', 'mcc_child')),
    baidu_user_id           text        NOT NULL DEFAULT '',
    user_name               text        NOT NULL DEFAULT '',
    parent_account_id       bigint      REFERENCES baidu_accounts (id) ON DELETE RESTRICT,
    app_id                  text        NOT NULL DEFAULT '',
    access_token_expires_at timestamptz,
    status                  text        NOT NULL DEFAULT 'active',
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, alias)
);

CREATE TABLE account_grants (
    id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    account_id bigint      NOT NULL REFERENCES baidu_accounts (id) ON DELETE CASCADE,
    permission text        NOT NULL CHECK (permission IN ('read', 'write')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, account_id)
);

CREATE TABLE credentials (
    id                 bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id          text        NOT NULL REFERENCES tenants (tenant_key) ON DELETE CASCADE,
    alias              text        NOT NULL,
    app_id             text        NOT NULL DEFAULT '',
    user_name          text        NOT NULL DEFAULT '',
    baidu_user_id      bigint      NOT NULL DEFAULT 0,
    status             text        NOT NULL DEFAULT 'active'
                                   CHECK (status IN ('active', 'pending', 'reauth_required', 'unconfirmed')),
    expires_at         timestamptz,
    refresh_expires_at timestamptz,
    is_default         boolean     NOT NULL DEFAULT false,
    nonce              bytea       NOT NULL,
    cipher             bytea       NOT NULL,
    key_version        integer     NOT NULL DEFAULT 1,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, alias)
);

CREATE UNIQUE INDEX idx_credentials_user
    ON credentials (tenant_id, baidu_user_id) WHERE baidu_user_id > 0;
CREATE UNIQUE INDEX idx_credentials_app
    ON credentials (tenant_id, app_id) WHERE app_id <> '';
CREATE UNIQUE INDEX idx_credentials_default
    ON credentials (tenant_id) WHERE is_default;

CREATE TABLE app_secrets (
    id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id  text        NOT NULL REFERENCES tenants (tenant_key) ON DELETE CASCADE,
    app_id     text        NOT NULL,
    nonce      bytea       NOT NULL,
    cipher     bytea       NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, app_id)
);

CREATE TABLE binding_states (
    id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    nonce      text        NOT NULL UNIQUE,
    tenant_id  text        NOT NULL REFERENCES tenants (tenant_key) ON DELETE CASCADE,
    subject    text        NOT NULL,
    alias      text        NOT NULL,
    app_id     text        NOT NULL DEFAULT '',
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_binding_states_expires ON binding_states (expires_at);

CREATE TABLE audit_log (
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
    payload_digest text        NOT NULL DEFAULT '',
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_audit_call_id ON audit_log (call_id);
CREATE INDEX idx_audit_tenant_time ON audit_log (tenant_id, started_at DESC);
CREATE INDEX idx_audit_user_time ON audit_log (user_sub, started_at DESC);

CREATE TABLE subscribe_events (
    id             bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    method         text        NOT NULL DEFAULT '',
    path           text        NOT NULL DEFAULT '',
    client         text        NOT NULL DEFAULT '',
    key_source     text        NOT NULL DEFAULT '',
    query          jsonb,
    headers        jsonb,
    body           text        NOT NULL DEFAULT '',
    body_truncated boolean     NOT NULL DEFAULT false,
    body_json      jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_subscribe_events_time ON subscribe_events (created_at DESC);

-- ---------------------------------------------------------------- 回灌

-- tenants：旧 id（"t_default"）搬进 tenant_key；旧表没有 updated_at，用 created_at 补
INSERT INTO tenants (tenant_key, name, plan, status, client_id, created_at, updated_at)
SELECT id, name, plan, status, client_id, created_at, created_at
FROM _bak_tenants;

-- users / baidu_accounts 的旧 id 是 uuid，需要映射到新的 bigint
CREATE TEMP TABLE _map_user (old_id text PRIMARY KEY, new_id bigint NOT NULL);
CREATE TEMP TABLE _map_account (old_id text PRIMARY KEY, new_id bigint NOT NULL);

DO $$
DECLARE r record; v_new_id bigint;
BEGIN
    FOR r IN SELECT * FROM _bak_users LOOP
        INSERT INTO users (tenant_id, idp_subject, email, display_name, status,
                           created_at, updated_at)
        VALUES (r.tenant_id, r.idp_subject, r.email, r.display_name, r.status,
                r.created_at, r.created_at)
        RETURNING id INTO v_new_id;
        INSERT INTO _map_user (old_id, new_id) VALUES (r.id, v_new_id);
    END LOOP;
END $$;

DO $$
DECLARE r record; v_new_id bigint;
BEGIN
    FOR r IN SELECT * FROM _bak_baidu_accounts LOOP
        INSERT INTO baidu_accounts (tenant_id, alias, owner_user_id, kind,
                                    baidu_user_id, user_name, app_id,
                                    access_token_expires_at, status,
                                    created_at, updated_at)
        VALUES (r.tenant_id, r.alias,
                (SELECT new_id FROM _map_user WHERE old_id = r.owner_user_id),
                r.kind, r.baidu_user_id, r.user_name, r.app_id,
                r.access_token_expires_at, r.status, r.created_at, r.updated_at)
        RETURNING id INTO v_new_id;
        INSERT INTO _map_account (old_id, new_id) VALUES (r.id, v_new_id);
    END LOOP;
END $$;

-- 父子关系在所有账户插入后再补（避免依赖旧表顺序）
UPDATE baidu_accounts a
SET parent_account_id = m2.new_id
FROM _map_account m1
JOIN _bak_baidu_accounts b ON b.id = m1.old_id
JOIN _map_account m2 ON m2.old_id = b.parent_account_id
WHERE a.id = m1.new_id AND b.parent_account_id IS NOT NULL;

INSERT INTO account_grants (user_id, account_id, permission, created_at, updated_at)
SELECT mu.new_id, ma.new_id, g.permission, g.created_at, g.created_at
FROM _bak_account_grants g
JOIN _map_user mu ON mu.old_id = g.user_id
JOIN _map_account ma ON ma.old_id = g.account_id;

INSERT INTO credentials (tenant_id, alias, app_id, user_name, baidu_user_id, status,
                         expires_at, refresh_expires_at, is_default, nonce, cipher,
                         key_version, created_at, updated_at)
SELECT tenant_id, alias, app_id, user_name, baidu_user_id, status,
       expires_at, refresh_expires_at, is_default, nonce, cipher,
       key_version, created_at, updated_at
FROM _bak_credentials;

INSERT INTO app_secrets (tenant_id, app_id, nonce, cipher, created_at, updated_at)
SELECT tenant_id, app_id, nonce, cipher, created_at, updated_at
FROM _bak_app_secrets;

-- issued_at 并入 created_at
INSERT INTO binding_states (nonce, tenant_id, subject, alias, app_id, expires_at,
                            created_at, updated_at)
SELECT nonce, tenant_id, subject, alias, app_id, expires_at, issued_at, issued_at
FROM _bak_binding_states;

-- 保留旧 id（日志有外部引用价值），需要 OVERRIDING SYSTEM VALUE
INSERT INTO audit_log (id, call_id, started_at, finished_at, duration_ms, tenant_id,
                       user_sub, user_name, app, account_alias, account_name,
                       capability, state, http_status, error_code, error,
                       request_params, response_body, request_bytes, response_bytes,
                       payload_digest, created_at, updated_at)
OVERRIDING SYSTEM VALUE
SELECT id, call_id, started_at, finished_at, duration_ms, tenant_id,
       user_sub, user_name, app, account_alias, account_name,
       capability, state, http_status, error_code, error,
       request_params, response_body, request_bytes, response_bytes,
       payload_digest, started_at, coalesce(finished_at, started_at)
FROM _bak_audit_log;

SELECT setval(pg_get_serial_sequence('audit_log', 'id'),
              coalesce((SELECT max(id) FROM audit_log), 0) + 1, false);

-- received_at 并入 created_at
INSERT INTO subscribe_events (id, method, path, client, key_source, query, headers,
                              body, body_truncated, body_json, created_at, updated_at)
OVERRIDING SYSTEM VALUE
SELECT id, method, path, client, key_source, query, headers,
       body, body_truncated, body_json, received_at, received_at
FROM _bak_subscribe_events;

SELECT setval(pg_get_serial_sequence('subscribe_events', 'id'),
              coalesce((SELECT max(id) FROM subscribe_events), 0) + 1, false);

-- ---------------------------------------------------------------- updated_at 触发器

CREATE TRIGGER trg_tenants_updated_at BEFORE UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_baidu_accounts_updated_at BEFORE UPDATE ON baidu_accounts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_account_grants_updated_at BEFORE UPDATE ON account_grants
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_credentials_updated_at BEFORE UPDATE ON credentials
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_app_secrets_updated_at BEFORE UPDATE ON app_secrets
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_binding_states_updated_at BEFORE UPDATE ON binding_states
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_audit_log_updated_at BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_subscribe_events_updated_at BEFORE UPDATE ON subscribe_events
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------- 清理备份

DROP TABLE _bak_tenants;
DROP TABLE _bak_users;
DROP TABLE _bak_baidu_accounts;
DROP TABLE _bak_account_grants;
DROP TABLE _bak_credentials;
DROP TABLE _bak_app_secrets;
DROP TABLE _bak_binding_states;
DROP TABLE _bak_audit_log;
DROP TABLE _bak_subscribe_events;
DROP TABLE _map_user;
DROP TABLE _map_account;
