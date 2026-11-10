# 任务：真实链路验证

> 勾选标准：能被命令或响应体证明，不靠"看起来对了"。
> 需要真实账号的条目标 `[live]`，没有账号时跳过，**不得伪造通过**。

## 1. 准备

- [ ] 1.1 `[live]` 在 <https://dev2.baidu.com> 建应用，拿到 AppID / SecretKey，填进 `deploy/.env`
- [ ] 1.2 `[live]` 确认回调地址已在应用里登记，且与 `MYBMO_RESOURCE_URL` 同域
      （地址已定：`https://baiduads.pandabase.top/oauth/baidu/callback`，代码侧
      `auth bind` + `GET /oauth/baidu/callback` 已就绪；**待在 dev2「安全设置」里登记**，
      并确认 infra 的 Caddy 把该路径也反代到容器）
- [x] 1.3 建 `docs/baidu-behavior.md` 骨架：身份与令牌、分页与上限、字段与写接口、错误码
      四组，结论列留空等实测

**出口**：`uv run mybmo-mcp auth status` 能读到应用凭据（不报 `unconfigured`）。

## 2. 绑定与账户

- [ ] 2.1 `[live]` 走 `auth bind` 完成授权，确认凭据文件已加密落盘（权限 600）
      （CLI 与回调端点已实现并本地验过失败路径；成功路径只差真实 AppID）
- [ ] 2.2 `[live]` `list_accounts` 返回该账户，且 `user_name` 是**中文名**而非登录名
- [ ] 2.3 `[live]` 用错误 `userName` 调一次账户查询，确认错误码是 81020 并记录文案
- [ ] 2.4 把 2.2 / 2.3 的结论写进 `docs/baidu-behavior.md`

**出口**：账户可见、`userName` 正确、81020 的触发条件有据可查。

## 3. 只读能力

- [ ] 3.1 `[live]` 计划列表：确认 `campaignTypes[]` 层级与字段驼峰，空列表时返回 `[]` 而非 `null`
- [ ] 3.2 `[live]` 关键词查询 `getKRByQuery`：数一次宽泛查询的返回条数，确认上限
- [ ] 3.3 `[live]` 报表：小时间范围拉一次，确认行数上限与分页字段
- [ ] 3.4 `[live]` 四个层级的暂停字段名（计划/单元/关键词/创意）各记录一次
- [ ] 3.5 把 3.1–3.4 回填 `descriptions.json` 的 `notes` / `params`
- [ ] 3.6 用真实响应替换 `tests/fixtures/report.json` 的占位分页字段，测试仍绿

**出口**：三类只读能力各有一次成功调用；能力说明里的上限/字段与实际一致。

## 4. 写能力（可回滚）

- [ ] 4.1 `[live]` 记录一个计划的原状态
- [ ] 4.2 `[live]` 暂停该计划，确认返回结构与成功判定字段
- [ ] 4.3 `[live]` 恢复该计划，确认状态回到 4.1
- [ ] 4.4 `[live]` 故意缺必填字段调一次，记录真实错误码与提示文案
- [ ] 4.5 把错误码分布写进 `docs/baidu-behavior.md`

**出口**：写路径可用且可回滚；必填字段与错误码不再靠猜。

## 5. `wait=True` 轮询

- [x] 5.1 `execute_capability` 的 `wait` 参数已存在；实现按 `async_task` 元数据轮询
      （`status_capability` / `result_capability` / `task_id_field` / `done_when`）
- [x] 5.2 轮询间隔递增（1s→2s→4s，上限 10s），总时长取元数据 `timeout`
- [x] 5.3 超时返回 `{ok: true, data: {timed_out: true, ...}}`，不抛错
- [x] 5.4 单测覆盖：立即完成、轮询后完成并取结果、超时、缺 task_id、API 异常（10 例）
- [ ] 5.5 `[live]` 对一个真实异步任务跑一次 `wait=True`，确认拿到终态
- [ ] 5.6 `[live]` 确认哪些能力是异步、任务字段名，把 `async_task` 补进 `capabilities.json`
      （**当前元数据不存在，5.5/5.6 是 `wait` 真正可用的前提**）

**出口**：三条分支有测试；真实异步任务能等到终态。

## 6. 真实客户端

- [ ] 6.1 在 IdP 给目标客户端建应用、登记回调、把 client_id 加进 `MYBMO_OIDC_AUDIENCE`
- [ ] 6.2 `[live]` Claude Desktop 或 Cursor 走完授权码登录，列出 5 个工具
- [ ] 6.3 `[live]` 调一次 `search_capabilities` 与一次 `execute_capability`，确认端到端可用
- [ ] 6.4 记下该客户端的回调地址与 client_id 用途（写进 `deploy/.env.example` 注释）

**出口**：不依赖脚本，真实客户端能接入并调用。

## 7. 收尾

- [ ] 7.1 `docs/baidu-behavior.md` 没有"待确认"残留
- [ ] 7.2 至少一条能力从 `experimental` 提升，并在能力表里注明验证依据
- [ ] 7.3 `README` 的「尚未验证」段落改为已验结论
- [ ] 7.4 全量测试 + `ruff` 绿

**出口**：v1 可以对外说"某几条能力实测可用"。
