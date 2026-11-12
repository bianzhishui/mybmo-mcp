-- 凭据落库：令牌与应用密钥从加密文件搬进 PostgreSQL
--
-- 设计见 docs/decisions/0007-credentials-in-postgres.md：
--   * 明文列只放**要查询或要约束**的字段：app_id / user_name / baidu_user_id / status / 过期时间
--   * secret_key / access_token / refresh_token / open_id / scope **只存在 cipher 里**
--   * 整块 AES-GCM 密文，AAD = mybmo-mcp/credential/v2/<tenant>/<alias>（带租户，挡跨租户搬行）
--   * key_version 目前恒为 1，只为将来引入每租户 DEK 预留
--   * 与 baidu_accounts 分工：credentials 是**凭据本体**，baidu_accounts 是**归属与展示**

CREATE TABLE IF NOT EXISTS credentials (
    tenant_id          text        NOT NULL,
    alias              text        NOT NULL,
    app_id             text        NOT NULL DEFAULT '',
    user_name          text        NOT NULL DEFAULT '',
    -- 百度侧稳定标识（实测 75102364）。用户可能改中文名，唯一性只能靠它
    baidu_user_id      bigint      NOT NULL DEFAULT 0,
    status             text        NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'pending', 'reauth_required', 'unconfirmed')),
    expires_at         timestamptz,
    refresh_expires_at timestamptz,
    -- 默认账户标记：文件后端用 data/accounts/default 文件表达，落库后用一个布尔列
    is_default         boolean     NOT NULL DEFAULT false,
    nonce              bytea       NOT NULL,
    cipher             bytea       NOT NULL,
    key_version        integer     NOT NULL DEFAULT 1,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, alias)
);

-- ADR 0006 D2：租户内一个百度账户只绑定一个应用。
-- 尚未拿到 userId 的 pending 行不参与判定（0 视为"未知"）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_credentials_user
    ON credentials (tenant_id, baidu_user_id) WHERE baidu_user_id > 0;

-- ADR 0006 D2 附加防呆：一个应用只服务一个账户
CREATE UNIQUE INDEX IF NOT EXISTS idx_credentials_app
    ON credentials (tenant_id, app_id) WHERE app_id <> '';

-- 每个租户最多一个默认账户
CREATE UNIQUE INDEX IF NOT EXISTS idx_credentials_default
    ON credentials (tenant_id) WHERE is_default;

-- 这两个列从建表起就没被写入过（001_init.sql 注释："凭据密文暂不落库"）。
-- 落库后同时存在两处凭据来源只会制造"谁是权威"的歧义，直接删掉。
ALTER TABLE baidu_accounts DROP COLUMN IF EXISTS secret_key_cipher;
ALTER TABLE baidu_accounts DROP COLUMN IF EXISTS refresh_token_cipher;

-- 应用密钥登记：预登记时还不知道 alias、也不知道百度账户，所以必须能按 app_id 独立存。
-- 只经 CLI `mybmo-mcp app secret set <app_id>` 写入（隐藏输入），MCP 工具层只收 appId。
CREATE TABLE IF NOT EXISTS app_secrets (
    tenant_id  text        NOT NULL,
    app_id     text        NOT NULL,
    nonce      bytea       NOT NULL,
    cipher     bytea       NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, app_id)
);
