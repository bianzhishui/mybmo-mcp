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
- [x] 2.7 接口目录：先按 133 服务 / 399 接口建了 `catalog.py` + `catalog.json`（源自 skill 的 api-index.md）。
      **P3 后整体删除**（catalog.py / catalog.json / build_catalog.py / test_catalog.py）：
      `capabilities.json` 从 SDK 内省出 129 服务 / 389 接口（差的 10 个是手写的上传、OAuth 等
      非 JSON 协议接口），职责完全覆盖，两份接口数据只会互相漂移。
      13 个能力域标签保留为 `scripts/service_groups.json`，只作生成器输入
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
- [x] 4.2 能力注册表：`baidu/capabilities.py` 加载 `capabilities.json`，按 key 索引，
      预建检索文本（key / 服务名 / 方法名 / 分组 / 中文别名）
- [x] 4.3 检索：`search_capabilities` 只返回短摘要。实现为**中文别名词典 + 加权子串匹配**
      （服务级别名 + 方法级别名共约 40 条），暂未引入 BM25——389 条规模下加权匹配够用，
      命中率不足时再上全文索引
- [x] 4.4 `get_capability_schema`：解开顶层 `$ref` 返回可读 schema，附带 `$ref` 闭包（只带用到的模型）
- [x] 4.5 `execute_capability`：参数按 schema 递归对齐（snake_case 自动转驼峰，未知字段拒绝并给候选），
      经 `transport.py` 发送（ADR 0005 结论：不用 SDK 发送）；**不做写操作拦截**
- [x] 4.6 账户归属校验：`auth/tenancy.py`（`Principal` / `Grant` / 三档权限 / 授权表加载）
      + `auth/access.py`（先筛后判，未知 alias 与无权 alias 同一条错误）。
      多用户模式下授权表缺失**直接报错**，不回退成人人可用；不传 alias 只在唯一账户时生效，
      不落进程级默认账户
- [x] 4.7 审计：`audit/log.py` JSONL 按天分文件，记录 `sub` / `tenant_id` / `account` /
      `capability` / `permission` / `call_id` / `state` / `payload_digest` / `duration_ms`。
      错误信息过 `redact()`，报文不落盘只留摘要；写失败打 stderr 但不中断调用
- [x] 4.8 统一响应封装沿用 `tools/_result.py` 的 `{ok,data,meta}` / `{ok,error}` 信封
- [ ] 4.9 测试：检索命中率、schema 生成、执行链路、参数错误提示**已完成**；
      越权拒绝见 `tests/test_tenancy.py`（覆盖默认拒绝、档位顺序、账户名探测、默认账户回落）
- [x] 4.10 生成 399 条能力骨架：**构建期**内省 SDK（递归下钻嵌套类型）生成 `capabilities.json`。
      实际产出 **389 条**（SDK 覆盖数，手写 10 个上传/OAuth 接口不在其中），
      585 个去重模型，`mutating` true=208 / false=180 / unknown=1（`PreAuditService.materialPreAudit` 待人工裁决）
- [x] 4.11 高频精写：`baidu/descriptions.json`，**50 条**覆盖账户 / 投放 / 关键词 / 报表 /
      信息流 / 批量六域。三类内容分开写：`description`（一句话，成为检索摘要）、
      `notes`（单位、层级、前置条件、易错点）、`params`（按 `campaignTypes[].budget`
      这样的路径写进 JSON Schema 的 `description`，agent 在 schema 里直接看到）。
      另修一个真 bug：别名表写成 `OpenapiReportService`，与实际 `OpenApiReportService`
      大小写不符，「报表」整组搜不到；已加 `validate_aliases()` 与用例守卫
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
      （文件式授权表没有租户维度，等 5.10 落 PostgreSQL 后由仓储层保证）
- [x] 7.3 已前移至 4.7：`audit/log.py` JSONL 追加，字段含 `tenant_id`、`account`、`sub`、
      `capability`、`call_id`、`state`、`payload_digest`
- [x] 7.4 已前移至 4.7：状态为 `succeeded` / `failed` / `unknown` / `denied`。
      与原文的四态差异：`attempted` 不单独记（一次调用只写一行终态，避免日志翻倍），
      另加 `denied`——越权时请求根本没发出去，记成 `failed` 会误判成百度侧故障
- [x] 7.5 已前移至 4.7：`tests/test_audit.py::test_error_message_is_redacted`
      与 `test_payload_is_not_stored_only_its_digest`
- [x] 7.6 `tests/test_tenancy.py`（跨租户/越权不可见）、`tests/test_audit.py`（令牌不入日志）

**出口**：A 租户的身份无法通过任何工具参数触达 B 租户的账户。

## 8. P7 上线

> 域名已定：`baiduads.pandabase.top`。Casdoor 的 issuer 预留 `auth.pandabase.top`。

- [x] 8.1 `deploy/docker-compose.yml`：**先只上 mcp + caddy 两个服务**。
      casdoor / postgres 的段落注释预留，理由见 8.7——`MYBMO_AUTH_MODE=local`
      不需要它们，1.9 GiB 的机器上也更稳
- [x] 8.2 `deploy/Caddyfile`：`baiduads.pandabase.top` → mcp:8000。
      `/healthz` 免鉴权（监控与容器探活用），其余路径走 `basic_auth`；
      `auth.pandabase.top` → Casdoor 的站点块注释预留
- [x] 8.3 `docs/operations.md`：前置条件、首次部署、客户端接入、健康检查、日志、
      备份恢复（数据卷 tar + `deploy/.env` 分开存）、升级回滚、Casdoor 切换清单、故障表。
      备份方式由原计划的每日 `pg_dump` 改为数据卷快照——PG 尚未引入
- [ ] 8.4 上线前逐项过 `docs/architecture.md` 第八节安全清单
- [ ] 8.5 监控：`/healthz` 已实现（`health.py`，含 `credentials` 字段），
      鉴权失败率与百度错误码分布未做——需要先把审计日志接出去
- [ ] 8.6 备份演练：从数据卷恢复到空实例，确认已绑定账户还能用
- [ ] 8.7 内存实测：确认 1.9 GiB 上 mcp + caddy 的实际占用，
      再决定是否同机塞 Casdoor + PostgreSQL（ADR 0002 未决问题）
- [ ] 8.8 `[live]` 在真实服务器上跑通：Caddy 签出证书 → `/healthz` 返回 200 →
      客户端通过 `mcp-remote` 连上并列出 5 个工具

**出口**：新机器按 `docs/operations.md` 可在 30 分钟内重建整套服务。

## 9. 需要真实账户才能推进的事项

以下逐项确认后才能把对应能力从 `experimental` 提升为稳定：

- [ ] 9.1 百度是否原样回传自定义 `state`
- [ ] 9.2 `refreshToken` 刷新后是否立即轮换（旧值何时失效）
- [ ] 9.3 报表接口单次返回上限与是否支持分页
- [ ] 9.4 计划 / 单元 / 关键词 / 创意各自的暂停字段名
- [ ] 9.5 `getKRByQuery` 单次返回上限（现有资料称 300，需实测）
- [ ] 9.6 写接口必填字段与真实错误码分布
