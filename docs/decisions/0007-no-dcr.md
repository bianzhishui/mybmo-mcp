# ADR 0007：不启用 DCR，客户端逐个显式注册

- 状态：已采纳（2026-11-08，实测后决定）
- 关联：[ADR 0006](0006-casdoor-audience.md)（`aud` 是 client_id）
- 影响任务：`tasks.md` 5.4（原计划"开 DCR + 回调白名单 + 频率限制"）、5.9（自助接入）

## 背景

`tasks.md` 5.4 的计划是：开启组织级 DCR（Dynamic Client Registration），
配置回调 URI 白名单与注册频率限制，让未知 MCP 客户端能自助接入（RFC 7591 流程）。

计划里假定 Casdoor 提供"白名单 + 限流"这两个控制项。实测发现**两个都不存在**。

## 实测事实

以下都在 `https://auth.pandabase.top` 上跑过，Casdoor `casbin/casdoor:latest`。

### 1. 策略只有两个取值

`organization.dcr_policy` 是 `varchar(100)`，前端 `OrganizationEditPage` 里是**一个开关**：

```js
onCheckedChange: e => n("dcrPolicy", e ? "open" : "disabled")
```

即只有 `open` / `disabled`，没有"白名单模式"。

### 2. 开启后任意回调地址都被接受

```bash
# 回环（正常客户端）
curl -X POST .../api/oauth/register -d '{"redirect_uris":["http://127.0.0.1:33418/callback"], ...}'
# → 201，创建应用 dcr_wh7gkj

# 敌意地址
curl -X POST .../api/oauth/register -d '{"redirect_uris":["https://evil.example.com/cb"], ...}'
# → 201，创建应用 dcr_nbpi0q
```

唯一的校验是"至少要有一个 URI"（缺 `redirect_uris` 才返回 400 `invalid_redirect_uri`）。
**没有域名白名单、没有回环限制、没有频率限制**——每次请求都新建一个 `application` 行。

这意味着授权码可以被引到任意域名。攻击形态是现成的：注册一个
`redirect_uri=https://attacker/cb` 的客户端，构造授权链接诱导已登录用户点击，
code 就会落到攻击者服务器上。

### 3. DCR 拿到的令牌用不了

`aud` 是每个客户端自己的 client_id（ADR 0006），而资源服务器只接受白名单里的 client_id：

```text
DCR 客户端 06fbc3... 的令牌 → aud = ["06fbc30987245dbe95e9"]
线上校验器（允许 60fda1...） → 被拒：令牌的 audience 不在允许的客户端列表里
```

Casdoor 也不支持改写这个字段：实测传 `resource=`（RFC 8707）和 `audience=`，
两种参数都被忽略，`aud` 恒等于 client_id。

**所以"开 DCR"不但没换来自助接入，还白开了一个可被利用的注册端点。**

## 决定

1. **`dcr_policy` 保持 `disabled`**。不提供匿名客户端注册入口。
2. **客户端逐个显式注册**：在 Casdoor 建应用，填唯一的回调地址，把它的 `clientId`
   加进 `MYBMO_OIDC_AUDIENCE`（逗号分隔）。
3. **`MYBMO_OIDC_AUDIENCE` 是白名单**，不再是单值。每个要调用本服务的客户端一行。
4. 回调白名单由**应用级** `redirect_uris` 承担（每个应用只有一个回调），
   而不是组织级策略——因为后者不存在。

新增客户端的最小步骤：

```text
1. Casdoor 控制台建应用，回调填该客户端固定地址（如 https://claude.ai/api/mcp/auth_callback）
2. 记下 clientId
3. deploy/.env 里 MYBMO_OIDC_AUDIENCE=<旧id>,<新id>
4. docker compose up -d mcp   （重启生效）
```

## 后果

**换来的**：市面上没有任何一个"注册即用"的入口能拿到有效令牌。陌生人注册成功也只是
在自己租户里多一条无用记录，且我们现在连这个入口都不开。

**代价**：

- **放弃 5.9 的"自助接入"目标**。MCP 规范设想的 DCR + 资源标识链路，
  在 Casdoor 上跑不通——缺的不是配置，是 RFC 8707 支持。
- 每接入一个客户端要改配置并重启 mcp（约 10 秒），不是零接触。
- 审计上仍能区分客户端（令牌的 `azp` / `client_id`），只是客户端集合由运维维护。

## 重新评估的触发条件

出现下面任意一条，这个决定应当被推翻：

- Casdoor 支持 RFC 8707 `resource` 参数，或允许应用配置固定的 `aud`
- Casdoor 给 DCR 加上回调域名白名单与限流（届时 `dcr_policy` 才不止两个值）
- 我们改用别的授权服务器（Keycloak、Authentik 对资源标识的支持更完整）

## 没有做的事

原计划里的"注册频率限制"没有实现，因为**入口没开**。若将来有人把 `dcr_policy`
改成 `open`，必须先在 Caddy 上加限流（`rate_limit` 是第三方模块，Ubuntu 包不自带，
要重新编译 Caddy），否则等于开放匿名建应用。
