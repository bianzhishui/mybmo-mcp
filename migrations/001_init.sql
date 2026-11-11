-- 多租户落库：初始结构
--
-- 设计要点（见 docs/architecture.md 第四节）：
--   * 账户是**私有**的：baidu_accounts.owner_user_id 记录绑定者，owner 隐式 admin
--   * account_grants 只表达 read / write —— admin 不能转授
--   * 所有业务表都带 tenant_id，隔离靠 SQL 条件而不是应用层 if
--   * 凭据密文暂不落库（仍在 data/credentials/*.enc），下面 *_cipher 列先留空

CREATE TABLE IF NOT EXISTS schema_version (
    version    integer     PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tenants (
    id         text        PRIMARY KEY,
    name       text        NOT NULL,
    plan       text        NOT NULL DEFAULT 'free',
    status     text        NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id           text        PRIMARY KEY,
    tenant_id    text        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    idp_subject  text        NOT NULL,
    email        text        NOT NULL DEFAULT '',
    display_name text        NOT NULL DEFAULT '',
    role         text        NOT NULL DEFAULT 'member'
                             CHECK (role IN ('owner', 'admin', 'member')),
    status       text        NOT NULL DEFAULT 'active',
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idp_subject)
);

CREATE TABLE IF NOT EXISTS baidu_accounts (
    id                  text        PRIMARY KEY,
    tenant_id           text        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    alias               text        NOT NULL,
    owner_user_id       text        NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    kind                text        NOT NULL DEFAULT 'standalone'
                                    CHECK (kind IN ('standalone', 'mcc_child')),
    baidu_user_id       text        NOT NULL DEFAULT '',
    user_name           text        NOT NULL DEFAULT '',
    parent_account_id   text        REFERENCES baidu_accounts (id) ON DELETE RESTRICT,
    app_id              text        NOT NULL DEFAULT '',
    -- 以下三列留给"凭据也落库"的那一天，现在恒为 NULL
    secret_key_cipher   text,
    refresh_token_cipher text,
    access_token_expires_at timestamptz,
    status              text        NOT NULL DEFAULT 'active',
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, alias)
);

CREATE TABLE IF NOT EXISTS account_grants (
    user_id    text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    account_id text        NOT NULL REFERENCES baidu_accounts (id) ON DELETE CASCADE,
    permission text        NOT NULL CHECK (permission IN ('read', 'write')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, account_id)
);

CREATE INDEX IF NOT EXISTS idx_users_tenant ON users (tenant_id);
CREATE INDEX IF NOT EXISTS idx_accounts_tenant ON baidu_accounts (tenant_id);
CREATE INDEX IF NOT EXISTS idx_grants_account ON account_grants (account_id);
