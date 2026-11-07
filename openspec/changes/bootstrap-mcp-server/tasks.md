# 实施任务

> 勾选标准：任务描述的行为可被命令或测试验证，不靠"看起来写完了"。
> 真实账户相关的任务统一标 `[live]`，无可用账号时可跳过，但不得伪造通过。

## 1. P0 骨架与契约

- [x] 1.1 补全 `pyproject.toml`：description、keywords、classifiers、`[tool.ruff]`（行宽 100、目标 py312）、`[tool.pytest.ini_options]`（`asyncio_mode = "auto"`、注册 `live` / `pg` marker）
- [x] 1.2 建目录：`src/mybmo_mcp/{tools,baidu,auth,approval,audit}`、`tests/`、`deploy/`，每包加 `__init__.py`
- [x] 1.3 实现 `server.py`：创建 `MCPServer` 实例（`mcp.server`，不是 `FastMCP`），`main()` 支持 `--transport {stdio,streamable-http}`、`--host`、`--port`，默认 stdio
- [x] 1.4 `mybmo-mcp --help` 可运行，输出包含两个传输方式的说明
- [x] 1.5 建 `.env.example`：`BAIDU_APP_ID`、`BAIDU_SECRET_KEY`、`MYBMO_DATA_DIR`、`CASDOOR_ISSUER`、`MYBMO_RESOURCE_URL`，全部留空并注释用途
- [x] 1.6 `.gitignore` 补充：`.env`、`*.jsonl`、`data/`、`*.pem`、`.workbuddy/`
- [x] 1.7 写 `tests/test_smoke.py`：导入包、调用 `main(["--help"])` 返回 0

**出口**：`uv sync && uv run pytest -q` 通过；`uv run ruff check .` 无告警。

## 2. P1 百度传输层

- [x] 2.1 `baidu/errors.py`：定义 `BaiduApiError`（code / position / service / method / hint）、`CredentialError`、`DryRunRequested`、`WriteNotPermitted`；错误信息脱敏
- [x] 2.2 `baidu/transport.py`：构造请求体 `{header:{userName, accessToken, action}, body:{...}}`，POST 到 `https://api.baidu.com/json/sms/service/{Service}/{Method}`，信息流走 `https://api.baidu.com/json/feed/v1/{Service}/{Method}`
- [x] 2.3 名称归一化：小写蛇形与驼峰都接受，`KR` 强制大写；对已规范的名字**幂等**。
      原计划「不做归一化」易读但不好用，实测归一化加 `KR` 特判更省心
- [x] 2.4 响应解析：校验 `header.status == 0`，否则抛 `BaiduApiError` 并带 `failures[0].code/message`
- [x] 2.5 重试策略：读 ≤2 次指数退避；写 0 次，超时抛 `state=unknown`
- [x] 2.6 记录 `rquota`，在响应里保留配额信息
- [x] 2.7 `baidu/catalog.py`：加载接口目录（Service / Method / 读写 / 产品线）。
      数据源改为 `src/mybmo_mcp/baidu/catalog.json`（随包分发，`scripts/build_catalog.py` 生成），
      不放在 `docs/` 是因为安装为 wheel 后取不到；内容沿用现有 skill 的 133/399 清单，标注"待与官方文档校验"
- [x] 2.8 `tests/test_transport.py`：注入 `httpx2.MockTransport`，覆盖成功、业务错误、网络超时、写不重试四个场景
- [x] 2.9 `tests/fixtures/` 放脱敏响应样例（账户、拓词、报表各一份）

**出口**：不联网即可证明请求体结构与错误归一化正确。

## 3. P2 账户、凭据与百度绑定

- [x] 3.1 `auth/models.py`：`Credential` dataclass（alias / user_name / user_id / app_id / secret_key / access_token / refresh_token / expires_at）
- [x] 3.2 `auth/vault.py`：本地实现走 AES-GCM 加密文件（`cryptography`）+ 权限 600，密钥来自 `MYBMO_MASTER_KEY`；接入 PG 后改存 `bytea` 列；两套实现同一接口
- [x] 3.3 账户别名规则：`^[a-z0-9][a-z0-9_-]{0,31}$`；文件名即身份，禁止 JSON 内的 alias 与文件名不一致
- [x] 3.4 `auth/registry.py`：`list_accounts()` 返回别名、账户中文名、是否可用、是否默认；**环境变量提供的凭据也要出现在列表里**
- [x] 3.5 损坏的凭据文件必须报错并列出路径，不静默跳过
- [x] 3.6 `auth/oauth.py`：换令牌 `POST u.baidu.com/oauth/accessToken`，刷新 `refreshToken`，`getUserInfo` 取中文名
- [x] 3.7 刷新加分布式锁（单机用文件锁 / 生产用 PG advisory lock），新令牌**先落库再使用**
- [x] 3.8 刷新失败标记 `reauth_required`，不重试旧令牌
- [x] 3.9 `auth/binding.py`：一次性 state（nonce + `sub` + `tenant` + TTL 15min），回调校验后换令牌、取用户名、落库
- [x] 3.10 state 不匹配时退化为按 `acct_<userId>` 命名并标记 `unconfirmed`，不静默接受
- [x] 3.11 换令牌失败**不回显 authCode**，错误信息不含令牌片段
- [x] 3.12 `tests/test_vault.py`、`test_registry.py`、`test_oauth.py`：覆盖多账户隔离、别名冲突、损坏文件、刷新轮换、state 重放被拒

**出口**：`[live]` 用一个真实账户完成一次绑定 → 刷新 → 调 `getUserInfo` 成功。

## 4. P3 能力层

> 定位调整见 [ADR 0003](../../../docs/decisions/0003-capability-only.md)：只提供能力，不做业务编排，不拦写。

- [x] 4.1 SDK POC：结论见 `docs/decisions/0005-sdk-poc.md`——SDK 可装、端点与序列化正确，
      但不校验未知字段、响应反序列化严格且不可关闭，因此**只作元数据源，不进运行时**
- [ ] 4.2 能力注册表：`catalog.json` 升级为 capability 清单，补参数 schema（从 SDK 内省）、
      中文别名、`mutating` 三态（true / false / unknown）
- [ ] 4.3 检索：中文别名词典 + BM25；`search_capabilities` 只返回短摘要，不返回 schema
- [ ] 4.4 `get_capability_schema`：按需返回完整 JSON Schema 与字段说明，命中缓存不重复生成
- [ ] 4.5 `execute_capability`：SDK 调用走 `asyncio.to_thread`、本地参数校验、错误归一化；
      **不做写操作拦截**
- [ ] 4.6 账户归属校验：`sub → account_grants`，alias 不在授权列表内直接拒绝（越权防线）
- [ ] 4.7 审计：记录 `sub` / `account` / `capability` / `request_id` / 结果状态
- [ ] 4.8 统一响应封装沿用 `tools/_result.py` 的 `{ok,data,meta}` / `{ok,error}` 信封
- [ ] 4.9 测试：检索命中率、schema 生成、执行链路、越权拒绝、参数错误提示
- [ ] 4.10 生成 399 条能力骨架：**构建期**内省 SDK（递归下钻嵌套类型）生成 `capabilities.json`，
      含端点路径、字段树、产品线、`mutating` 初判。**每个能力必须有非空描述**，允许是骨架级
- [ ] 4.11 高频精写：账户 / 投放 / 关键词 / 报表四域约 50 条，补参数语义、金额单位、父子层级
- [ ] 4.12 `execute_capability(wait=True)`：按能力的 `async_task` 元数据轮询；
      超时返回 `{timed_out: true}` 而非报错（见 ADR 0004 D5）

**出口**：不联网即可用 fixture 跑通 search → schema → execute 全链路；越权账户被拒；
每个能力都有非空描述（骨架级或精写）。

## 5. P4 Casdoor 接入

- [ ] 5.1 `deploy/casdoor/docker-compose.yml`：Casdoor + PostgreSQL，管理台端口只绑 `127.0.0.1`
- [ ] 5.2 `deploy/README.md`：首次启动、改默认口令、建管理员、确认 issuer 的操作步骤
- [ ] 5.3 验证 discovery：`registration_endpoint` 与 `client_id_metadata_document_supported` 的实际取值记录到 `docs/casdoor-notes.md`
- [ ] 5.4 开启组织级 DCR（`dcrPolicy`），配置回调 URI 白名单与注册频率限制
- [ ] 5.5 `auth/resource.py`：JWT 校验（JWKS 缓存、`iss` / `aud` / `exp` / `scope`），失败返回 401 并带 `WWW-Authenticate`
- [ ] 5.6 发布 `/.well-known/oauth-protected-resource`，`resource` 与 `authorization_servers` 正确
- [ ] 5.7 scope 设计：`baidu:read` / `baidu:write` / `baidu:admin`；权限不足返回 403 + `insufficient_scope`
- [ ] 5.8 `tests/test_resource.py`：伪造签名、错 audience、过期、缺 scope 四种拒绝路径
- [ ] 5.9 `[live]` 用 Claude Desktop / Cursor 贴 URL 完成自助注册 → 登录 → 调用一个能力
- [ ] 5.10 `tenants` / `users` / `account_grants` 三表与仓储层：`sub → tenant` 映射，
      按 grant 判定可执行档位（详见 ADR 0004 D1/D3）
- [ ] 5.11 MCC 子账户：`kind=mcc_child` 沿 `parent_account_id` 取令牌，`userName` 用子账户中文名；
      解绑主账户前先处理子账户
- [ ] 5.12 权限档位与 `mutating` 联动：`read` 拒绝写能力，`mutating=unknown` 按写处理

**出口**：未知客户端可自助接入；跨服务令牌被拒；跨租户账户不可见，越权按 grant 拒绝。

## 6. P5 写操作审批（已取消）

按 [ADR 0003](../../../docs/decisions/0003-capability-only.md)，本服务不再承担写审批：
`execute_capability` 忠实执行，是否要求人工确认由 skill 工作流决定。

原 6.1–6.9 中只有「账户归属校验」保留，已并入 4.6；其余（`approval_id`、`change_preview`、
`change_execute`、写前预检、默认暂停）不再实施。编号保留，便于对齐历史记录。

## 7. P6 多租户与审计

- [x] 7.1 已前移至 5.10：租户三表在 P4 建，因为按 grant 的授权校验在 P4 就要用
- [ ] 7.2 所有查询强制带 `tenant_id`，用仓储层封装而非散落的 if 判断
- [ ] 7.3 `audit/log.py`：JSONL 追加，字段含 `tenant_id`、`account_id`、`sub`、`tool`、`request_id`、`state`、`payload_digest`
- [ ] 7.4 四态：`attempted` / `succeeded` / `failed` / `unknown`；失败也留痕
- [ ] 7.5 脱敏断言：审计与日志中不得出现 `access_token`、`refresh_token`、`secret_key`
- [ ] 7.6 `tests/test_tenancy.py`、`test_audit.py`：跨租户账户不可见、令牌不入日志

**出口**：A 租户的身份无法通过任何工具参数触达 B 租户的账户。

## 8. P7 上线

- [ ] 8.1 `deploy/docker-compose.yml`：casdoor + mybmo-mcp + postgres，仅 Caddy 暴露 80/443
- [ ] 8.2 `deploy/Caddyfile`：`auth.example.com` → Casdoor，`mcp.example.com` → server
- [ ] 8.3 `docs/operations.md`：启动、备份（每日 `pg_dump`）、升级、回滚、日志位置
- [ ] 8.4 上线前逐项过 `docs/architecture.md` 第八节安全清单
- [ ] 8.5 监控最低要求：健康检查端点、鉴权失败率、百度错误码分布
- [ ] 8.6 备份演练：从 `pg_dump` 恢复到空实例，确认账户还能用

**出口**：新机器按 `docs/operations.md` 可在 30 分钟内重建整套服务。

## 9. 需要真实账户才能推进的事项

以下逐项确认后才能把对应能力从 `experimental` 提升为稳定：

- [ ] 9.1 百度是否原样回传自定义 `state`
- [ ] 9.2 `refreshToken` 刷新后是否立即轮换（旧值何时失效）
- [ ] 9.3 报表接口单次返回上限与是否支持分页
- [ ] 9.4 计划 / 单元 / 关键词 / 创意各自的暂停字段名
- [ ] 9.5 `getKRByQuery` 单次返回上限（现有资料称 300，需实测）
- [ ] 9.6 写接口必填字段与真实错误码分布
