# ADR 0007：凭据（令牌与应用密钥）落 PostgreSQL，不再存加密文件

- 状态：已接受
- 日期：2026-11-12
- 决策者：项目负责人
- 关联：[ADR 0006](0006-multi-app-credentials.md)（应用凭据随凭据走）、
  [ADR 0004](0004-multi-tenant-accounts.md)（多租户账户模型）

## 背景

现行凭据存在加密文件里：`/data/accounts/<alias>.json`，一个 alias 一个文件，
AES-GCM 密文 + `nonce`，AAD 绑定 alias，文件 `0600`、目录 `0700`、原子写。
存储介质是 docker named volume `mybmo_data`（主机
`/var/lib/docker/volumes/mybmo_data/_data`）。

同时 PG 里已有 `baidu_accounts` 表，但 `secret_key_cipher` / `refresh_token_cipher`
两列**恒为 NULL**（`001_init.sql` 注释："凭据密文暂不落库"）。结果是凭据与账户
分居两处：要回答"这个账户用的哪个应用"只能翻 vault，PG 答不了。

要求是把凭据搬进 PG，不再存文件。顺带解决两个既有缺陷：

1. **刷新竞态**：文件锁按 alias 加，多进程下不可靠
2. **绑定半失败**：`binding.complete()` 先写凭据、再登记 owner，中间失败会让
   账户落库却对所有人不可见（代码注释已承认此风险）

## 决策

### D1 一张 `credentials` 表：明文列承载查询与约束，密文块承载机密

```sql
CREATE TABLE IF NOT EXISTS credentials (
    tenant_id          text        NOT NULL,
    alias              text        NOT NULL,
    app_id             text        NOT NULL,
    user_name          text        NOT NULL DEFAULT '',
    baidu_user_id      bigint      NOT NULL DEFAULT 0,
    status             text        NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'pending', 'reauth_required', 'unconfirmed')),
    expires_at         timestamptz,
    refresh_expires_at timestamptz,
    -- 默认账户：文件后端用 data/accounts/default 文件表达，落库后用一个布尔列
    is_default         boolean     NOT NULL DEFAULT false,
    nonce              bytea       NOT NULL,
    cipher             bytea       NOT NULL,
    key_version        integer     NOT NULL DEFAULT 1,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, alias)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_credentials_user
    ON credentials (tenant_id, baidu_user_id) WHERE baidu_user_id > 0;
CREATE UNIQUE INDEX IF NOT EXISTS idx_credentials_app
    ON credentials (tenant_id, app_id) WHERE app_id <> '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_credentials_default
    ON credentials (tenant_id) WHERE is_default;
```

- 明文列只放**需要查询或约束**的字段：`user_name` / `app_id` / `baidu_user_id` /
  `status` / 两个过期时间
- `secret_key` / `access_token` / `refresh_token` / `open_id` / `scope`
  **只存在 `cipher` 里**，不建明文列、不建索引
- 两个部分唯一索引落实 ADR 0006 D2 的 1:1 约束。`WHERE baidu_user_id > 0`
  让尚未拿到 `userId` 的 `pending` 行不参与唯一性判定

### D2 加密沿用整块 AES-GCM，AAD 改为「租户 + 别名」

- 算法、密钥长度、`nonce` 长度全部沿用 `FileVault`（AES-GCM，12 字节 nonce）
- **AAD 从 `mybmo-mcp/credential/v1/<alias>` 改为 `mybmo-mcp/credential/v2/<tenant>/<alias>`**
  （**只对 PG 后端生效**；`file` 后端继续用 v1，不重加密存量文件）
  ——落库后"行"更容易被搬动，AAD 带上 `tenant_id` 可以挡住跨租户复制密文
- `key_version` 起始为 1。**当前不支持轮换**，该列只为将来引入每租户 DEK 预留升级位
- 否决明文入库：一次 `pg_dump` 就是全量令牌泄露

### D3 双后端：`MYBMO_CREDENTIAL_BACKEND=file|pg`

- 与 `MYBMO_TENANCY_BACKEND`、`MYBMO_AUDIT_BACKEND` 保持同一套路
- `file`（默认）继续用 `FileVault`，本地开发与单测不依赖 PG
- `pg` 用于生产；两者必须接口一致（`load` / `save` / `scan` / `delete` / `get` / `list`）
- 抽象出一个 `CredentialStore` 协议，`AccountRegistry` 与 `CredentialService` 只依赖协议，
  不直接引用 `FileVault`

### D4 刷新串行改用数据库锁

```sql
SELECT ... FROM credentials WHERE tenant_id = $1 AND alias = $2 FOR UPDATE;
```

刷新在**同一事务内**完成"读 → 判定是否需刷新 → 调百度 → 写回"，
让并发刷新自然串行。理由：ADR 0006 实测确认百度每次刷新都会轮换
`refreshToken`，两个并发刷新会让其中一方拿到已作废的令牌。
文件锁做不到跨容器/跨进程，DB 行锁可以。

落地形态：`PgCredentialStore.locked(alias)` 在事务内 `FOR UPDATE` 并交出**可写句柄**
（`row.save(credential)` 写在同一事务里——另开连接会等自己持有的行锁而死锁）。
`CredentialService` 用鸭子类型探测：有 `locked()` 就走行锁，没有就退回文件锁；
文件锁仍然保留（同机串行，无害）。

### D5 绑定落库事务化

`binding.complete()` 的成功路径改为单事务：

```text
BEGIN
  INSERT/UPDATE credentials   (令牌、状态 active)
  UPSERT baidu_accounts       (alias, owner_user_id, app_id, baidu_user_id)
  -- owner 登记与凭据写入同生共死
COMMIT
```

消除"凭据写进去了但账户对所有人不可见"的半失败状态。

### D6 应用密钥单独一张 `app_secrets` 表

ADR 0006 D3 的折中方案规定 `secretKey` 只经服务器 CLI 输入（`app secret set <app_id>`，
隐藏输入），MCP 只收 `appId`。**预登记时还没有 alias、也还不知道是哪个百度账户**，
所以密钥必须能按 `app_id` 独立存——`credentials` 行做不到（它的主键是 alias），
因此单独一张表：

```sql
CREATE TABLE IF NOT EXISTS app_secrets (
    tenant_id  text        NOT NULL,
    app_id     text        NOT NULL,
    nonce      bytea       NOT NULL,
    cipher     bytea       NOT NULL,   -- app_id / secret_key / scope / note
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, app_id)
);
```

- AAD 与文件后端一致：`mybmo-mcp/app/v1/<app_id>`（只绑 app_id，不含租户）——
  这条密钥登记是"应用级"资产，跟凭据行分开加密的边界
- 绑定流程：`bind_account(alias, app_id)` 从该表取密钥 → **预写一条
  `status='pending'` 的 `credentials` 行**（只含应用凭据、无令牌）→ 签发链接；
  回调按 alias 取回这把密钥换令牌 → 回填并置 `active`
- `list_accounts` 必须过滤 `status='pending'`，否则用户会看到一个永远不可用的账户

### D7 删除 `baidu_accounts` 的两个死列

`secret_key_cipher` / `refresh_token_cipher` 从未被写入过，落库后会造成
"两处都能存凭据、但不知道谁是权威"。`003` 迁移里一并 `DROP COLUMN`。
`baidu_accounts` 保留为**账户归属与展示**表（`alias` / `owner_user_id` / `user_name` /
`app_id` / `baidu_user_id`），凭据本体只在 `credentials`。

### D8 迁移：一次性搬 + 逐条回读校验

```text
for each /data/accounts/*.json:
    解密（旧 AAD: .../v1/<alias>）
    重新加密（新 AAD: .../v2/<tenant>/<alias>）
    INSERT INTO credentials
    回读该行 → 解密 → 与源凭据逐字段比对
全部通过 → 切换 MYBMO_CREDENTIAL_BACKEND=pg → 归档（不删除）原文件目录
```

- 校验不通过则**整体回滚**，不切换后端
- 实现见 `scripts/migrate_credentials_to_pg.py`：`--dry-run` / `--verify-only`，
  逐字段比对（时间容差 1 秒），单条失败不中断整批，最终退出码反映是否有失败
- 旧文件保留至少一个发布周期，确认无问题后再删
- `MYBMO_MASTER_KEY` 不变，迁移是重加密而非换密钥

## 备选方案

| 方案 | 否决理由 |
|---|---|
| 明文入库，靠 PG 权限与网络隔离 | 备份、`pg_dump`、只读账号都会成为令牌泄露面 |
| 分字段列存储（token 各占一列） | 需要多列加密或明文；整块密文更简单，也便于整体版本化 |
| 每租户 DEK（本轮） | 轮换成本更低，但多一套 DEK 管理与包装逻辑；先用 `key_version` 预留，等真需要轮换时再上 |
| 双写过渡 | 凭据是低频写、强一致的数据，双写只制造不一致 |
| 保留文件后端作为生产主存储 | 与本次要求相反；且跨实例部署时文件方案不可用 |

## 后果

### 正面

- 账户与应用信息同库，`UNIQUE(tenant, baidu_user_id)` 可在数据库层强制
- 刷新用行锁串行，彻底解决 `refreshToken` 轮换竞态
- 绑定、owner 登记原子化，消除半失败
- 多实例部署成为可能（凭据不再是本机文件）

### 负面 / 需要承担

| 风险 | 说明 | 应对 |
|---|---|---|
| **`MYBMO_DATABASE_URL` 密码未轮换** | 该密码已在对话中明文出现（`docker inspect` 时脱敏遗漏）。落库后它等于"全部令牌"的访问权 | 记为**高优先级未决项**，尽快轮换并确认 PG 只监听容器网络 |
| 主密钥仍与密文同机 | `MYBMO_MASTER_KEY` 在 compose env 明文，密文在同机 PG | 加密只防备份/误传，不防主机沦陷；将来考虑 KMS 注入 |
| `pg_dump` 含全部密文 | 备份文件成为敏感资产 | 备份加密存储、限制访问；`key_version` 留出换钥路径 |
| 刷新事务持有行锁期间要等百度响应 | 慢响应会占住锁 | 设 `statement_timeout`；锁只覆盖单账户，不影响其他账户 |
| `pending` 行可能残留 | 用户放弃授权会留下无令牌行 | 与 state TTL 对齐（900 秒）；同 alias 重绑直接覆盖 |

## 未决问题

- [ ] **轮换 `MYBMO_DATABASE_URL` 的密码**（本次明确暂不处理，但凭据落库后风险等级上升）
- [ ] 备份策略：`pg_dump` 是否加密、保存在哪里、保留多久
- [ ] 是否引入每租户 DEK（`key_version` 已预留）
- [ ] `credentials` 与 `audit_log` 的保留期是否一致（审计里可能含账户名）
- [ ] **租户维度目前取 `MYBMO_TENANT_ID`（默认 `t_default`），不是按 `principal.tenant_id` 分实例**。
      单租户部署没问题，真上多租户前必须改成按请求身份解析，否则跨租户会读到同一个租户的凭据
