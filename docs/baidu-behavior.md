# 百度接口行为实测

只记**结论与依据**，不记尝试过程。验证方法见
[`openspec/changes/live-validation/design.md`](../openspec/changes/live-validation/design.md)。

**状态标记**：`未验证` 表示仍是推断、不能写进能力说明；`已验证` 必须附依据（响应片段或命令）。

## 一、身份与令牌

| 问题 | 结论 | 依据 |
|---|---|---|
| `userName` 取什么 | **已验证：令牌所属账户的中文名**（本账户为 `baidu-YF电商-玛卡快消01`），不是登录名 | 2026-11-11 绑定后 `list_accounts` 返回该值，且用它调 `CampaignService.getCampaign` / `KRService.getKRByQuery` 均成功 |
| 回调带回哪些参数 | **已验证：带回授权码、`state` 与 `userId`**（`userId` 有值才可能换到令牌，`userId=0` 时百度报 `code=3`） | 2026-11-11 真实授权一次，凭据以 state 里的别名 `main` 落地、`status=active`；字段名仍按多候选兼容 |
| 81020 的触发条件 | **已验证：请求头 `userName` 不是令牌所属账户中文名**。返回 `code=81020, position=_user, message="The username information in the request is incorrect."`，`header.status=2`、`succ=0` | 2026-11-11 用 `userName=绝对不存在的账户名` 调 `AccountService.getAccountInfo` |
| 自定义 `state` 是否原样回传 | **已验证：原样回传**（绑定按 state 里的别名落库，未退化成 `acct_<userId>`） | 2026-11-11 真实授权一次，`/data/accounts/main.json` 生成、`binding_state/` 被消费 |
| `refreshToken` 刷新后是否立即轮换 | 未验证（决定刷新失败能否重试旧值） | — |
| `accessToken` 有效期 | **已验证：1 天**（`expiresIn=86400`），`refreshToken` 30 天。与「文档称 30 天」不符 | 2026-11-11 落库凭据：access 剩 1.0 天、refresh 剩 30.0 天 |
| 应用级凭据能否跨账户使用 | 未验证（推断：同一 AppID 下多账户各自授权） | — |

## 二、分页与上限

| 问题 | 结论 | 依据 |
|---|---|---|
| 报表 Enum 列的返回形态 | **过滤用 key，返回 value**（`campaignType` 传 "1" 返回"商品"） | 2026-11-11 电商 600002 / 600029 实测 |
| 报表 `filters` | **生效**：`{column: campaignId, operator: IN, values: [...]}` 可把报告收窄到单计划 | 2026-11-11 600004 / 600010 / 600011 实测 |
| 报表时间区间上限 | **824 天**，超出报 `901606 时间区间不能超过824天!` | 2026-11-11 传 2024-01-01~2026-11-09 实测 |
| 报表单次返回行数上限 | **未触顶**：`rowCount=10000` 请求 30 天数据只回 30 行（受 `totalRowCount` 约束），真实上限仍未测到 | 2026-11-11 一站式多渠道报告实测 |
| 报表是否支持分页（字段名） | **已验证**：请求 `startRow` + `rowCount`，响应 `rowCount`（本次行数）/ `totalRowCount`（总行数）。**默认排序不稳定**（相邻两页日期不连续），取全量必须先传 `sorts` | 2026-11-11 30 天 DAY 实测：`rowCount=5` → 5 行/总数 30，`startRow=5` → 换了一页 |
| `getKRByQuery` 单次返回上限 | **已验证：300 条**，`data` 是扁平词对象数组 | 2026-11-11 `query=跨境电商` 正好返回 300 条，字段含 `word/competition/recBid/pcPV/mobilePV/show/click/recommendPricePc/recommendPriceMobile/pv/groupName...` |
| 计划列表是否分页 | **分两套，不一样**：搜索 `getCampaign` 无分页参数（`campaignFields/campaignIds/mobileExtend/adType`），空结果 `[]`；电商 `getCampaignShopping` 用 **`limit: [offset, size]`**，且**必传**（不传报 8602） | 2026-11-11 实测：`limit=[0,5]` 与 `[5,5]` 返回不重叠；电商计划共 35 条 |

## 三、字段与写接口

| 问题 | 结论 | 依据 |
|---|---|---|
| 计划的暂停字段名 | **已验证（电商推广）：`pause` 布尔值**，`false` 启用 / `true` 暂停；与 `status` 一致（`status=0` 启用 24 条、`status=2` 暂停 11 条） | 2026-11-11 `CampaignShoppingService.getCampaignShopping` 全量 35 条取样 |
| 搜索推广计划的暂停字段名 | 未验证（该账户搜索侧 `getCampaign` 返回 `[]`，无计划可取样） | — |
| 单元的暂停字段名 | 未验证（SDK 里没有 shopping 的单元/创意**读**接口，只有 campaign 级） | — |
| 关键词的暂停字段名 | 未验证 | — |
| 创意的暂停字段名 | 未验证 | — |
| 写接口必填字段清单 | 未验证（已知缺 `campaignShoppingTypes` 时统一报 8602 `_sys`，百度不指出缺哪个字段） | — |
| 写操作是否幂等 | **已验证幂等**：对已暂停的计划再次 `pause=true`，返回 `succ=1 oprs=1 status=0 failures=[]`，回读 `pause` 仍为 `true`、`status` 仍为 `2` | 2026-11-11 `updateCampaignShopping` 对 2079251858（珠宝专用清洗剂9.7）实测 |
| 写请求的成功判定字段 | **`header.status==0` 且 `failures==[]` 且 `succ==oprs>=1`**；失败时 `status=2`、`succ=0`、`failures[{code,position,message}]`。响应头恒带 `quota`（本次消耗）与 `rquota`（剩余配额，实测 200000000）、`traceid` | 2026-11-11 成功（updateCampaignShopping）与失败（81020）各一次对照 |
| 写操作的生效时延 | **同步生效**：`updateCampaignShopping` 返回后 1 秒回读即是新状态（`pause`/`status` 同时翻转），没有异步等待 | 2026-11-11 恢复→复原各一次，中间态与终态均回读确认 |
| 暂停/恢复是否可回滚 | **可以，且净效果为零**：`pause=false` → `status` 0；`pause=true` → `status` 2；两次往返后与原状态完全一致 | 2026-11-11 计划 2079251858：原 `pause=true/status=2` → 恢复 `false/0` → 复原 `true/2` |
| `budget=0` 的语义 | **不限预算**（该账户 35 条计划全是 `0.0`） | 运营确认，非接口实测；写验证时不改动此字段 |
| 计划的名称字段 | 电商计划是 **`campaignShoppingName`**；`campaignName` / `name` 不合法（静默忽略，不报错但也不返回） | 2026-11-11 三个候选字段各试一次 |
| 异步任务如何查终态（能力名 + 判定字段） | **已验证**：`OpenApiReportService.createReportTask` 返回 `taskId` + `taskStatus=RUNNING` → `getTaskStatus(taskId)` 轮询到 `taskStatus=SUCCESS`，终态响应**直接带** `fileUrl`（CSV）、`tableHeader`、`dataStartRow`。两个接口的响应都是单元素数组。实测 ~0.7 秒完成，`wait=True` 轮询 2 次命中 | 2026-11-11 实测 |

## 四、错误码基线

| 错误码 | 含义 | 触发条件 | 来源 |
|---|---|---|---|
| 81020 | `The username information in the request is incorrect.`（`position=_user`） | 请求头 `userName` 不是令牌所属账户的中文名 | 已验证：2026-11-11 错误 `userName` 实测 |
| 9011519 / 1519 | `Request field is invalid`，`position` 指到具体字段 | 字段名不在接口白名单；`AccountService.getAccountInfo` 不传 `accountFields` 也报此码 | 已验证：2026-11-11 四种 `accountFields` 组合实测（仅 `userId` 通过）；报表接口里 `position=_user` 是笼统提示，不代表真跟用户有关 |
| 8602 | `We are unable to serve your request, please try again later.` | `getCampaignShopping` **不传 `limit`**（不是网络问题，重试无用） | 已验证：2026-11-11 缺省 `limit` 时必现，加上即正常 |
| 901400 | `Reporttype could not be empty` / `Unknow reportType: N` | `getReportData` 不传 `reportType`，或传了不存在的值。**实测 1–12 全部 `Unknow`，真实枚举未知** | 已验证：2026-11-11 reportType 1–12 扫描 |
| 30002 | `应用不存在`（OAuth 层，AppID 无效） | 用不存在的 `appId=app-demo` 调 `oauth/accessToken` | 已验证：2026-11-10 本地回调路由实测，返回 `code=30002 message=应用不存在`（说明授权回调 → 换令牌这条链路本身是通的，只差真实 AppID） |
| 其他 | 未验证 | — | — |

## 五、维护约定

1. 每条结论必须能指到依据：一次响应片段、一条命令、或 `tests/fixtures/` 里的样本。
2. 结论变了就改这一行，不追加"补充说明"——保持每次读到的是当前事实。
3. 能力级细节（单位、层级、前置条件）不写这里，写进 `baidu/descriptions.json` 的
   `notes` / `params`，让 agent 在 schema 里直接看到。
4. **验证过一条能力，就在 `descriptions.json` 给它加 `verified` 块**（本文只记行为结论，
   不承担"哪些能力能承诺"这件事）：

   ```json
   "CampaignShoppingService.updateCampaignShopping": {
     "verified": {
       "at": "2026-11-11",
       "level": "write",
       "account": "baidu-YF电商-玛卡快消01",
       "basis": "计划 2079251858 暂停→恢复→复原，succ=1/oprs=1，回读状态一致"
     }
   }
   ```

   `level` 只能是 `read`（只读验过）/ `write`（写路径验过且可回滚）/ `partial`（只验了一部分）。
   检索摘要会显示 ✅（read/write）或 ◐（partial），`/healthz` 的 `verified` 字段是总数。
   **没打标不等于不能用**，只表示"没验过，别一次批量写"。
