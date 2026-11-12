# ADR 0006：应用凭据随凭据走，一个百度账户在租户内只绑定一个应用

- 状态：已接受
- 日期：2026-11-12
- 决策者：项目负责人
- 关联：[ADR 0004](0004-multi-tenant-accounts.md)（多租户账户模型）、[ADR 0003](0003-capability-only.md)（能力层）、
  [ADR 0007](0007-credentials-in-postgres.md)（凭据落库，取代加密文件）、
  [`docs/auth-contract.md`](../auth-contract.md)

## 背景

ADR 0004 把账户建模为「租户资产 + grant 授权」，但把**应用凭据**（`appId` / `secretKey`）
当成服务级全局配置：`Settings.baidu_app_id` / `baidu_secret_key` 来自环境变量，
`Runtime.oauth` 是单例 `BaiduOAuth`，`BindingManager` 也吃同一组。

实际形态是：**一个百度账户下可以有多个应用**，每个应用一组 `appId` / `secretKey`，
各自授权、各自换令牌。凭据必须跟着应用走，而不是跟着服务走。

本次范围收窄后，本项目只支持一种形态：

- 一个百度账户**只能绑定一个应用**（百度本身允许多绑，这里是我们加的更严约束）
- 一个应用**只对应一个百度账户**
- **不做 MCC 子账户**，不建模 `parent_account_id`
- 一个用户可绑定多个百度账户（多条 alias，各自带自己的应用凭据）

## 实测事实（2026-11-12，pandabase-dev01 真实绑定）

这些结论来自一次真实授权，不是文档推断，是本 ADR 的立论基础：

| 观测 | 结论 |
|---|---|
| 回调 query = `appId` + `authCode` + `signature` + `state` + `timestamp` + `userId` | 百度**原样回传 `state`**，且**自带 `appId` / `userId`** |
| `authCode` 是 JWT，payload 含 `uid` / `appId` / `platformId` / `iss=商业开发者中心` | `platformId` 在授权码里就有，可据此判定平台维度 |
| 同一 (app, 用户) 授权两次，两条凭据的 `refreshToken` **不同且同时有效**（实测 23.0h / 24.0h） | 百度允许同 (app, 用户) 存在**多套并行令牌**；新授权不会作废旧的 |
| `main`（2026-11-11 绑定）的 `app_id` 已是 `20426c04…fe45` | `Credential.to_storage()` 一直存着应用凭据，**零迁移** |
| `access_token` 有效期约 24 小时 | 每个账户每天刷新一次 |

同时测出两个既有缺陷（列入改造清单）：

1. **`authCode` 进了 docker 日志**。`server.py:112` 注释声称"绝不记查询参数"，但 uvicorn 0.54
   的 access log 会把 query string 完整打印（`get_path_with_query_string`）。
2. **PG 的 `app_id` / `baidu_user_id` 是死列**。`tenancy.py:303-305` 的 `register_account()`
   只写 `alias / owner / user_name`，这两列从未被写入。

## 决策

### D1 应用凭据是凭据的一部分，不再有全局应用

`Credential` 的 `app_id` / `secret_key`（已加密落库）是**唯一权威**。
删除「全局应用」这个概念：

- `Runtime.oauth` 从 `cached_property` 单例改为按凭据现造
- `BindingManager` 不再持 `app_id` / `secret_key` 成员，改为每次绑定传入
- 环境变量 `BAIDU_APP_ID` / `BAIDU_SECRET_KEY` **废弃**。存量凭据已自带 app 凭据，
  不需要回退逻辑——留着 fallback 等于给"刷新用错应用"这个 bug 留后门

### D2 绑定关系 1:1，租户内强制

```text
UNIQUE (tenant_id, baidu_user_id)     -- 一个百度账户在租户内只绑一个应用
UNIQUE (tenant_id, app_id)            -- 附加防呆：一个应用只服务一个账户
UNIQUE (tenant_id, alias)             -- 调用句柄（维持 ADR 0004 不变）
```

- `baidu_user_id` 是百度侧稳定标识（实测 `75102364`），用它做唯一键而不是 `user_name`
  —— 中文名可改
- `UNIQUE(tenant, app_id)` 在业务上其实是冗余的（应用归属于该账户，别家也授权不了），
  但代价极低，用来挡住"两个账户争抢同一个应用"的误操作
- **约束在应用层与 PG 两层都做**。PG 侧需要先把 `baidu_user_id` 回填（见 D7）

### D3 绑定入参带应用与 scope，`platformId` 写死

```text
bind_account(account_alias, app_id, secret_key, scope="")
build_authorize_url(app_id, callback, state, scope, platform_id=PLATFORM_ID)
```

- `app_id` / `secret_key` **只经 CLI 输入，隐藏回显**，绝不经 MCP 工具参数——
  工具参数会进对话记录与审计，等于把密钥写在聊天里。落地形态：
  `mybmo-mcp app secret set <appId>` 登记密钥（`app secret list` 只看是否已登记），
  `mybmo-mcp auth bind --alias <别名> --app-id <appId>` 生成链接，MCP 的
  `bind_account(alias, app_id)` 只传 appId
- `scope` 是应用级参数，存进凭据，并在授权链接里带上
- `platformId` 固定为常量 `4960345965958561794`（实测授权码 claims 里就是这个值）
- **`secretKey` 重置说明**：重置后是否会影响已签发的 `refreshToken` **尚未实测**，
  见未决问题

### D4 回调取应用凭据有两条路，都失败才报错

`/oauth/baidu/callback` 是全局单一路由，回调阶段必须知道该用哪把 `secretKey`：

1. **首选：`state` → alias → 凭据**（实测百度确实回传 `state`）
   生成授权链接时**预写一条 `status=pending` 的占位凭据**（含 `app_id` / `secret_key`、
   无令牌），`state` 只带 alias；回调按 alias 从 vault 读回应用凭据去换令牌
2. **兜底：回调的 `appId` + `userId`**（实测回调自带这两个参数）
   按 `appId` 找已登记的应用，配上 `userId` 定位账户
3. **两条都不可用 → 失败**。绝不按 `userId` 猜命名成 `acct_<userId>`——
   多应用下猜错会把凭据存到错误的应用名下，且事后无法识别

这与现行 `binding.py` 的行为不同：现行在 `state` 缺失时用 `acct_<userId>` + `unconfirmed` 兜底，
是因为当时无法从回调拿到应用信息。实测证明回调带 `appId`，兜底路可以走得更准。

占位凭据的配套要求：

- `pending` 状态不得出现在 `list_accounts` 的可选项里，必须过滤或显式标注
- 占位记录 **不登记 tenancy owner**（未完成的绑定不应产生有权限的资产），回调成功后再登记
- 与 state 的 TTL 对齐（900 秒），过期视同无效；同 alias 重新绑定时直接覆盖

### D5 刷新用凭据自己的应用凭据，锁粒度回到 alias

```python
# 修复 CredentialService.ensure_valid（现行是错的）
bundle = await BaiduOAuth(cred.app_id, cred.secret_key).refresh(cred.refresh_token, cred.user_id)
```

现行 `service.py:64` 调的是全局单例 `self._oauth`，忽略 `cred.app_id`。
一旦某条凭据不是用 env 那组授权的，`refreshToken` 就换不出令牌，并被
`mark_reauth_required` 打成假"授权失效"，用户只能反复重绑。

因为 D2 保证了账户与应用 1:1，不存在"一把令牌被多个账户共享"，
锁粒度维持在 alias 即可，不需要引入 `(app, user)` 级锁。

### D6 唯一性冲突在回调阶段检测，拒绝而非替换

授权前拿不到 `baidu_user_id`，冲突只能在换来令牌、调到 `getUserInfo` 之后才知道：

```text
exchange(app 凭据, authCode)
  → getUserInfo（拿 masterName / userId）
  → 查 UNIQUE(tenant, baidu_user_id) 与 UNIQUE(tenant, app_id)
  → 冲突：丢弃令牌、不落库、删占位记录，返回错误
  → 通过：回填令牌、置 active、登记 owner
```

错误文案（**本项目自加的约束，用户没有心理预期，必须写清楚**）：

> 该百度账户已绑定应用「{app_id 前 8 位}…」（别名 `{alias}`）。
> 本项目规定一个百度账户在租户内只能绑定一个应用，如需换用其他应用，
> 请先执行 `mybmo-mcp auth unbind {alias}`。

**不做自动替换**：静默替换会让一组授权无声失效，且用户不知道发生了什么。

### D7 PG 投影补齐 + 数据回填

- `register_account()` 增加 `app_id`、`baidu_user_id` 两个入参并落库，
  否则 `baidu_accounts` 那两列永远是空，唯一约束也无从校验
- 迁移：遍历 vault 中现有凭据，按其 `app_id` / `user_id` 回填 PG 对应行
- **权威仍在 vault**：PG 是投影，用于约束校验与批量查询；两者不一致时以 vault 为准并告警

### D8 日志不得含授权码

- uvicorn access log 关闭，或加中间件把 query string 从日志里摘掉
- 回调路由自己**只记状态码与文案**（现行注释的意图是对的，只是被 access log 破了功）
- 回调的 `signature` 参数暂不校验：伪造回调不可行（假 `authCode` 换不到令牌），
  记为低优先级待办

## 备选方案

| 方案 | 否决理由 |
|---|---|
| 保留全局 `appId`/`secretKey`，多应用时改环境变量重启 | 同租户内无法并存两组应用；每次变更让全部已绑账户失效 |
| 独立的 App 实体 + 独立 Token 实体（本项目曾讨论） | 1:1 约束下应用、令牌、账户三者可并回一条记录；多一层表只增加不一致来源 |
| 支持 MCC 子账户（ADR 0004 D2） | 本期范围外；`kind` / `parent_alias` 不建模 |
| 回调 `state` 缺失时按 `userId` 命名兜底 | 多应用下会存到错误应用名下，且事后无法识别 |
| 冲突时自动替换旧绑定 | 静默让一组授权失效，用户无从感知 |
| 允许同一百度账户绑多个应用 | 会产生 N 把并行可用令牌（实测已证实百度允许），导致操作无法归因、越权写、配额重复占用 |

## 后果

### 正面

- 一个租户内每个百度账户只有一条授权，审计与归因唯一
- 修掉「刷新用错应用」这个会产生假"授权失效"的真 bug
- 表结构基本不动：`Credential` 早已存 `app_id` / `secret_key`，**存量凭据零迁移**

### 负面 / 需要承担

| 风险 | 说明 | 应对 |
|---|---|---|
| 约束比百度原生更严 | 百度允许多应用并存，我们禁止 | 错误文案写清原因与解绑方式（D6） |
| 换应用要解绑 | 想换应用必须 unbind 再 bind | CLI 提供 `auth unbind`，文档写清 |
| 回调期才发现冲突 | 用户已完成授权才被拒，体验有损耗 | 文案给出唯一动作（先解绑），并保留已授权信息供重试 |
| `pending` 占位残留 | 用户放弃授权会留下无令牌凭据 | TTL 对齐 state（900s），同 alias 重绑直接覆盖 |
| PG 与 vault 双份状态 | 回填遗漏会导致约束校验失效 | 迁移脚本 + 一致性校验任务 |

## 已结案的未决项

- [x] **`secretKey` 重置后旧令牌是否失效**：2026-11-12 实测**全部失效**。
      重置后用新密钥刷 `main` 报 `50003 token验签失败`，业务接口报
      `89406 The access token you provided is invalidate.`。结论：换密钥 = 一次
      **全员重绑**事件，运维要提前通知
- [x] **`user_name` 改名后的 `81020`**：改为**刷新令牌时顺手 `getUserInfo` 校正**
      （`CredentialService._refresh`）。取不到就沿用旧名，不影响刷新本身
- [x] **`app_id` 展示**：`AccountEntry.app_id` 只输出脱敏前缀（`20426c04…`）
- [x] **`auth bind --tenant` 默认值**：缺省取 `MYBMO_TENANT_ID`，不再留空

## 未决问题

- [ ] 回调 `signature` 暂不校验：百度未公开算法，且伪造回调还需有效 `authCode`
      （换令牌必失败），攻击面小。先把 `signature` / `timestamp` 记进
      `docs/baidu-behavior.md` 的样本，等有算法再补
- [ ] 同一百度账户在**不同租户**里各绑一次是否允许（当前约束是租户内，跨租户不拦）
