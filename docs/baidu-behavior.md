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
| 报表单次返回行数上限 | 未验证 | — |
| 报表是否支持分页（字段名） | 未验证 | — |
| `getKRByQuery` 单次返回上限 | **已验证：300 条**，`data` 是扁平词对象数组 | 2026-11-11 `query=跨境电商` 正好返回 300 条，字段含 `word/competition/recBid/pcPV/mobilePV/show/click/recommendPricePc/recommendPriceMobile/pv/groupName...` |
| 计划/单元列表是否分页 | **已验证：`getCampaign` 没有分页参数**（`ApiCampaignQueryRequest` 仅 `campaignFields/campaignIds/mobileExtend/adType`）；空结果返回 `[]` 而非 `null` | 2026-11-11 实测该账户 `getCampaign` 返回 `[]` |

## 三、字段与写接口

| 问题 | 结论 | 依据 |
|---|---|---|
| 计划的暂停字段名 | 未验证（账户内暂无计划，`getCampaign` 返回 `[]`，无法取样） | — |
| 单元的暂停字段名 | 未验证 | — |
| 关键词的暂停字段名 | 未验证 | — |
| 创意的暂停字段名 | 未验证 | — |
| 写接口必填字段清单 | 未验证 | — |
| 写操作是否幂等（重复提交会怎样） | 未验证 | — |
| 异步任务如何查终态（能力名 + 判定字段） | 未验证 | — |

## 四、错误码基线

| 错误码 | 含义 | 触发条件 | 来源 |
|---|---|---|---|
| 81020 | `The username information in the request is incorrect.`（`position=_user`） | 请求头 `userName` 不是令牌所属账户的中文名 | 已验证：2026-11-11 错误 `userName` 实测 |
| 9011519 | `Request field is invalid`，`position` 指到具体字段 | 字段名不在接口白名单；`AccountService.getAccountInfo` 不传 `accountFields` 也报此码 | 已验证：2026-11-11 四种 `accountFields` 组合实测（仅 `userId` 通过） |
| 30002 | `应用不存在`（OAuth 层，AppID 无效） | 用不存在的 `appId=app-demo` 调 `oauth/accessToken` | 已验证：2026-11-10 本地回调路由实测，返回 `code=30002 message=应用不存在`（说明授权回调 → 换令牌这条链路本身是通的，只差真实 AppID） |
| 其他 | 未验证 | — | — |

## 五、维护约定

1. 每条结论必须能指到依据：一次响应片段、一条命令、或 `tests/fixtures/` 里的样本。
2. 结论变了就改这一行，不追加"补充说明"——保持每次读到的是当前事实。
3. 能力级细节（单位、层级、前置条件）不写这里，写进 `baidu/descriptions.json` 的
   `notes` / `params`，让 agent 在 schema 里直接看到。
