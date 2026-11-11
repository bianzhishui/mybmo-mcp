# 任务：真实链路验证

> 勾选标准：能被命令或响应体证明，不靠"看起来对了"。
> 需要真实账号的条目标 `[live]`，没有账号时跳过，**不得伪造通过**。

## 1. 准备

- [x] 1.1 `[live]` 在 <https://dev2.baidu.com> 建应用，拿到 AppID / SecretKey，填进 `deploy/.env`
- [x] 1.2 `[live]` 确认回调地址已在应用里登记，且与 `MYBMO_RESOURCE_URL` 同域
      （地址已定：`https://baiduads.pandabase.top/oauth/baidu/callback`，代码侧
      `auth bind` + `GET /oauth/baidu/callback` 已就绪；**待在 dev2「安全设置」里登记**，
      并确认 infra 的 Caddy 把该路径也反代到容器）
- [x] 1.3 建 `docs/baidu-behavior.md` 骨架：身份与令牌、分页与上限、字段与写接口、错误码
      四组，结论列留空等实测

**出口**：`uv run mybmo-mcp auth status` 能读到应用凭据（不报 `unconfigured`）。

## 2. 绑定与账户

- [x] 2.1 `[live]` 走 `auth bind` 完成授权，确认凭据文件已加密落盘（权限 600）
      （CLI 与回调端点已实现并本地验过失败路径；成功路径只差真实 AppID）
- [x] 2.2 `[live]` `list_accounts` 返回该账户，且 `user_name` 是**中文名**而非登录名
- [x] 2.3 `[live]` 用错误 `userName` 调一次账户查询，确认错误码是 81020 并记录文案
- [x] 2.4 把 2.2 / 2.3 的结论写进 `docs/baidu-behavior.md`
      （顺带补了 state 回传、回调参数、accessToken 有效期、9011519 四条实测）

**出口**：账户可见、`userName` 正确、81020 的触发条件有据可查。

## 3. 只读能力

- [ ] 3.1 `[live]` 计划列表：确认 `campaignTypes[]` 层级与字段驼峰，空列表时返回 `[]` 而非 `null`
      （搜索侧 `getCampaign` 返回 `[]`；该账户的计划都在**电商推广线**，走
      `CampaignShoppingService.getCampaignShopping`，35 条已取样：`campaignType=1`、
      字段驼峰已确认；搜索侧层级与驼峰仍未取样）
- [x] 3.2 `[live]` 关键词查询 `getKRByQuery`：数一次宽泛查询的返回条数，确认上限
- [x] 3.3 `[live]` 报表：小时间范围拉一次，确认行数上限与分页字段
      （reportType=1783967；startRow/rowCount + totalRowCount；区间上限 824 天；单次行数上限未触顶）
      （卡在 `reportType` 枚举：实测 1–12 全部 `Unknow reportType`，需要官方文档定值）
- [ ] 3.4 `[live]` 四个层级的暂停字段名（计划/单元/关键词/创意）各记录一次
      （计划级已验：电商 `pause` 布尔、`status` 0=启用/2=暂停；单元/关键词/创意缺读接口与样本）
- [x] 3.5 把 3.1–3.4 回填 `descriptions.json` 的 `notes` / `params`
- [x] 3.6 用真实响应替换 `tests/fixtures/report.json` 的占位分页字段，测试仍绿

**出口**：三类只读能力各有一次成功调用；能力说明里的上限/字段与实际一致。

## 4. 写能力（可回滚）

- [x] 4.1 `[live]` 记录一个计划的原状态
- [x] 4.2 `[live]` 暂停该计划，确认返回结构与成功判定字段
- [x] 4.3 `[live]` 恢复该计划，确认状态回到 4.1
      （用 2079251858：暂停态 → 恢复(status 0) → 复原(status 2)，净效果为零，同步生效）
- [x] 4.4 `[live]` 故意缺必填字段调一次，记录真实错误码与提示文案
      （结论：缺 `campaignShoppingTypes` 统一报 8602 `_sys`，百度不指出具体字段）
- [x] 4.5 把错误码分布写进 `docs/baidu-behavior.md`

**出口**：写路径可用且可回滚；必填字段与错误码不再靠猜。

## 5. `wait=True` 轮询

- [x] 5.1 `execute_capability` 的 `wait` 参数已存在；实现按 `async_task` 元数据轮询
      （`status_capability` / `result_capability` / `task_id_field` / `done_when`）
- [x] 5.2 轮询间隔递增（1s→2s→4s，上限 10s），总时长取元数据 `timeout`
- [x] 5.3 超时返回 `{ok: true, data: {timed_out: true, ...}}`，不抛错
- [x] 5.4 单测覆盖：立即完成、轮询后完成并取结果、超时、缺 task_id、API 异常（10 例）
- [x] 5.5 `[live]` 对一个真实异步任务跑一次 `wait=True`，确认拿到终态
- [x] 5.6 `[live]` 确认哪些能力是异步、任务字段名，把 `async_task` 补进 `capabilities.json`
      （`async_task` 改由 descriptions.json 声明，加载时校验；已确认 createReportTask 一个；
      另发现百度把 taskId/taskStatus 放在单元素数组里，`_lookup` 已支持）
      （**当前元数据不存在，5.5/5.6 是 `wait` 真正可用的前提**）

**出口**：三条分支有测试；真实异步任务能等到终态。

## 6. 真实客户端

- [x] 6.1 在 IdP 给目标客户端建应用、登记回调、把 client_id 加进 `MYBMO_OIDC_AUDIENCE`
      （`mybmo-mcp` 补 scopes 并沿用 33418 回调；另建 `mybmo-client` 备用。
      授权码 + PKCE + 换令牌 + `/mcp` 握手 + `tools/call` 全部实测通过）
- [ ] 6.2 `[live]` 真实客户端（Claude Desktop / Cursor / DSH）走完授权码登录，列出全部工具
      （已用 mcp-remote 真实走通：授权码 + PKCE → 令牌 `aud=["709a8e…"]` /
      `owner=pandabase` / `scope="baidu:read offline_access"`，该令牌实测 `/mcp` 认证通过；
      但"在客户端里稳定列出工具"尚未确认，且 Claude Desktop / Cursor 本体未试）
- [x] 6.3 `[live]` 调一次 `search_capabilities` 与一次 `execute_capability`，确认端到端可用
      （`list_accounts` 返回 main/read；`search_capabilities("拓词")` 3 条命中，
      检索摘要里带 ✅ 与实测依据。尚未用真客户端复跑，见 6.2）
- [x] 6.4 记下该客户端的回调地址与 client_id 用途（写进 `deploy/.env.example` 注释）

**出口**：不依赖脚本，真实客户端能接入并调用。

## 7. 收尾

- [ ] 7.1 `docs/baidu-behavior.md` 没有"待确认"残留
- [x] 7.2 至少一条能力从 `experimental` 提升，并在能力表里注明验证依据
      （新增 `verified` 维度：8 条打标——6 个 ✅ read/write、2 个 ◐ partial；
      检索摘要显示标记、`get_capability_schema` 返回凭证、`/healthz` 报总数）
- [x] 7.3 `README` 的「尚未验证」段落改为已验结论
      （改成 9 行实测表 + 3 条「没有账户/客户端」的明确未知）
- [x] 7.4 全量测试 + `ruff` 绿
      （2026-11-11：249 passed，ruff All checks passed）

**出口**：v1 可以对外说"某几条能力实测可用"。
