# 电商广告投放 官方接口速查（tmp/ 官方文档 2026-11-13）

本文是 `tmp/` 下 25 篇百度官方文档（电商广告投放线）的蒸馏版，用于补全
`descriptions.json` 与 `get_capability_schema` 的能力描述。原始抓取件不入库（`.gitignore` 的 `tmp/`）。

> 优先级：**能力是否可调用以 `capabilities.json` 为准；字段语义/取值范围以本文（官方文档）为准。**
> 标 `⚠` 的是实测与官方文档可能不一致或需注意的点；标 `❌ SDK 未覆盖` 的是当前能力表里**没有**、需手写补入的接口。

## 一、能力覆盖对照

| 官方文档 | 能力键 | 当前能力表 | 读写 |
|---|---|---|---|
| 查询计划 | `CampaignShoppingService.getCampaignShopping` | ✅ | 读 |
| 添加计划 | `CampaignShoppingService.addCampaignShopping` | ✅ | 写 |
| 更新计划 | `CampaignShoppingService.updateCampaignShopping` | ✅ | 写 |
| 删除计划 | `CampaignShoppingService.deleteCampaignShopping` | ✅ | 写 |
| 计划批量复制 | `CampaignShoppingService.copyCampaignShopping` | ✅ | 写 |
| 查询主播信息 | `CampaignShoppingService.getAnchor` | ✅ | 读 |
| 查询兴趣列表 | `CampaignShoppingService.getInterests` | ✅ | 读 |
| 查询商品信息 | `CampaignShoppingService.getProduct` | ✅ | 读 |
| 查询账户 | `AccountShoppingService.getAccountShopping` | ✅ | 读 |
| 更新账户 | `AccountShoppingService.updateAccountShopping` | ✅ | 写 |
| 电商推荐人群 | `CrowdShoppingService.getEshopTradeCrowds` | ✅ | 读 |
| 查询/添加/更新/删除智投项目 | `ProjectShoppingService.*` | ❌ SDK 未覆盖 | 读/写 |
| 定向包管理（8 个） | `AtpService.*` | ❌ SDK 未覆盖 | 读/写 |
| 评论管理（2 个） | `CommentShoppingService.*` | ❌ SDK 未覆盖 | 读/写 |
| 电商定向设置 | （audience 字段释义，无独立接口） | — | — |

> `❌ SDK 未覆盖` 的三个服务需要走 `scripts/handwritten_capabilities.json` 手写补入（见第七~九节），
> 否则 `get_capability_schema` 查不到、`execute_capability` 无法调用。

**只读接口实测（2026-11-13，账户 `baidu-YF电商-玛卡快消01`，均已补 `verified`）：**
`getCampaignShopping`、`getAtps`、`getAnchor`、`getInterests`、`getProduct`、
`getAccountShopping`、`getEshopTradeCrowds`、`getProjectShopping`、`getCommentInfoList` 均调用成功（无数据返回 `[]`）。
⚠️ `AtpService.getAtpList` 官方无必填，但实测返回 `8602`（本账户无定向包/疑服务未开通），定向包读取请用 `getAtps`。

## 二、电商计划 `CampaignShoppingService`

### 2.1 `getCampaignShopping`（查询计划）

入参（`limit`、`campaignShoppingFields` 必填）：

| 字段 | 类型 | 说明 | 限制 |
|---|---|---|---|
| `campaignShoppingIds` | Long[] | 计划 ID 列表 | [0,1000]，空=全部 |
| `sortField` | String | 排序字段 | 查询的字段均可排序 |
| `isDesc` | Boolean | 是否倒序 | true 倒序 / false 正序 |
| `limit` | Integer[] | 分页参数 | [2,2]，`[offset,size]`，offset 从 0 起 |
| `campaignShoppingFields` | String[] | 查询字段 | 见下枚举 |
| `filters` | FilterCondition[] | 过滤 | 仅支持 `('planIdName','like')` |

`campaignShoppingFields` 官方枚举（字段 - 含义）：

```
campaignShoppingId 计划ID | status 计划状态 | campaignShoppingName 计划名称 | pause 是否暂停
targetType 推广目标 | eshopType 商品店铺类型 | productId 商品ID | productName 商品名称
anchorId 主播ID | budget 预算 | transType 转化目标 | ocpcBid 出价
startTime 开始推广日期 | endTime 结束推广日期 | schedule 推广时段 | ideaType 创意来源
atpShoppingId 定向包ID | sex 性别 | age 年龄 | customAge 自定义年龄 | regionType 地域类型
region 地域 | keywords 意图词 | keywordsExtend 意图搜索行为 | crowd 人群
excludeTrans 排除人群已转化用户 | excludeCrowd 排除人群自定义 | fcKeywordWtpids 关键词包
intelligentWeight 智能放量 | brand 品牌名称 | userPortrait 品牌图片 | title 标题
description 描述 | pictures 图片 | videos 视频 | broadcastCovers 直播间封面
roi 商品支付ROI | useLiftBudget 一键起量
```

返回关键枚举：

- `status`：0 有效 / 1 处在暂停时段 / 2 暂停 / 3 预算不足 / 10 账户预算不足 / 11 账户余额为零 / 20 审核中 / 21 审核不通过 / 22 部分有效 / 30 商品已失效
- `targetType`：1 商品 / 2 直播
- `eshopType`：1 度小店 / 4 百度健康商城
- `transType`：14 订单支付成功 / 90 商品支付成功 / 80 直播间成单 / 81 涨粉 / 82 观看 / 83 直播间商品按钮点击 / 84 停留 / 10090 商品支付ROI
- `campaignType`（出价模式）：1 控成本 / 4 放量
- `ideaType`（创意来源）：1 自定义 / 2 直播间实况（商品推广只能为 1）
- `campaignSource`（创建模式）：0 常规新建 / 1 批量新建 / 2 批量复制 / 3 AI 自动新建（新建后不可改）
- `intelligentType`：0 自定义投放 / 1 智能投放

> ⚠ 实测补充：`fengchaoTitle` 可读回（`#{文案}`）；`fengchaoDescription` **读不回**；
> `campaignShoppingFields` 里放非法字段名**不报 1519，而是静默忽略**（见 `docs/campaign-shopping.md`、`baidu-behavior.md`）。

### 2.2 `addCampaignShopping`（添加计划）

入参顶级：`campaignShoppingTypes`（CampaignShoppingType[]，集合 [1,20]）。

`campaignShoppingTypes[]` 必填：`campaignShoppingName`、`targetType`、`budget`、`transType`、`ocpcBid`、`audience`、`ideaType`、`material`、`campaignType`。

| 字段 | 类型 | 说明 / 限制 |
|---|---|---|
| `campaignShoppingName` | String | 计划名，[1,100]，不可重复，汉字算 2 字符 |
| `targetType` | Integer | 1 商品 / 2 直播 |
| `eshopType` | Integer | 1 度小店 / 4 百度健康商城（推广商品时必填） |
| `productId` | String | 商品中心 ID（推广商品时必填） |
| `anchorId` | Long | 主播 ID（推广主播时必填） |
| `budget` | Double | [50, 9999999.99]，0=不限；放量模式必须 >0 |
| `transType` | Integer | 转化类型（枚举见 2.1；度小店 [14,90]，健康商城 [90]，直播间 [80,81,82,83,84]） |
| `ocpcBid` | Double | [0.2, 99999]，两位小数 |
| `startTime` / `endTime` | String | 日期；endTime 空=长期，`9999-01-01`=长期 |
| `schedule` | Schedule[] | [0,84]，空=全天，一天 ≤12 段（weekDay/startHour/endHour） |
| `audience` | Map<String,String> | 定向设置（见第三节） |
| `ideaType` | Integer | 1 自定义 / 2 直播间实况（商品只能 1） |
| `material` | Material | 创意内容（直播实况时用；更新必须全量） |
| `campaignType` | Integer | 出价模式 1 控成本 / 4 放量 |

`material` 关键限制：`title` [1,12] 每条 9-60 字符；`description` [0,6] 每条 9-80（商品推广必传，直播不传）；
`pictures` 声明集合 [1,36]，但**实际可用 ≤18 张**（商品推广与自定义直播推广都是 18；36 只是字段声明上限，
别当可用张数）；图片 ID 不重复；商品尺寸 370×245/1140×640/750×1000，自定义直播推广仅 1140×640；
`videos` [0,12]（≥6 秒、≤500MB、16:9 或 9:16，需封面）；`broadcastCovers` [0,1]（直播用，1140×640）。
`addCampaignShopping` 与 `updateCampaignShopping` 是同一个计划资源，上述尺寸/张数规则**两者一致**。

> ⚠ **`material.userPortrait`（品牌头像，Picture，120×120）官方没标必填，实测必带**（2026-11-13，账户
> `baidu-YF电商-玛卡快消01`，商品推广 `targetType=1`/`ideaType=1`）：
> 不带它 → `901217`「物料错误：创意设置品牌头像错误」，`failures[].field=userPortrait`
> （call_id `f544250ba6ae`）；补上 120×120 头像 `{imageId:15505964077}` 后该错消失、计划建成功
> `campaignShoppingId 2387079078`（call_id `98b453c47249`）。
> 官方原文 `添加计划` 对必填字段用「必填」后缀标注，`userPortrait` 没有——而 `添加智投项目`/`更新智投项目`
> 写的是「userPortrait必填」，**官方两处口径不一致**。商品推广按必带处理；直播推广（`targetType=2`）
> 与其他账户未验证，别一律当成协议必填。
>
> ⚠ 同一轮实测：`audience` 只传 `sex/age/region/regionType/keywords/keywordsExtend/intelligentWeight`
> 会报 `901216`「兴趣错误」（`field=interests`），要同时带上 `crowd`/`customAge`/`interests`/
> `etradecrowds`/`excludeCrowd` 等扁平键。

### 2.3 `updateCampaignShopping`（更新计划）

入参顶级：`campaignShoppingTypes`。`campaignShoppingId` **必填**，其余只传要改的字段。
可写字段与新增基本一致（去掉 targetType/eshopType/productId/transType），**`material` 是整体替换**。

> ⚠ 读回来的对象带一堆只读字段（`reAuditStatus`/`auditId`/`smallSize`…），回传前必须裁剪，
> 否则报「不是该能力的参数」。详见 `docs/campaign-shopping.md` 第二节。

### 2.4 `deleteCampaignShopping`（删除计划）

入参：`campaignShoppingIds`（Long[]，必填）。删除不可逆，连带预算/创意失效。
> ⚠️ 官方文档还列了 `optFrom`（web常规 / 列表页批量 / API接口 / `BATCH_OPT`），但 **SDK 的 `DeleteCampaignRequest` 没有这个字段**，传了会被参数校验拒（`optFrom 不是该能力的参数`，2026-11-13 实测）——**别传**。

### 2.5 `copyCampaignShopping`（计划批量复制）

入参：`campaignShoppingId`（Long[]，计划 ID）、`copyNum`（Integer，复制份数）。
返回：`campaignShoppingId`、`totalCount`、`successCount`、`failCount`、`status`（1 成功/2 失败/3 部分成功）、`validatorErrorList[]`、`successIds`（新计划 ID 列表）。

### 2.6 `getAnchor`（查询主播信息）

入参：空对象。返回：`anchorId`、`anchorName`、`headPortrait`（头像 URL）、`transInfos[].transType`（可选转化类型）。

### 2.7 `getInterests`（查询兴趣列表）

入参：空对象。返回：`id`、`name`、`children[]`（`TreeNode`，下一级；只传最下级兴趣 ID）。

### 2.8 `getProduct`（查询商品信息）

入参（`eshopType`、`limit` 必填）：`eshopType`（1 度小店/4 百度健康商城）、`limit`（[offset,size]）、
`filters`（`field` 传**字面量** `name|id` + `op:like`，或 `field:id` + `op:equal`）、`isDesc`、`isIntelligent`（是否 AI 投手）。
> ⚠️ 实测（2026-11-13）：`field:name` + `op:like` 会**返回空数组**——不是「过滤不生效」，是字段名必须写 `name|id` 这个字面量。
返回：`eshopType`、`id`、`name`、`logo`、`salesVolume`、`price`、`updateTime`、`transInfos[]`、
`recommendDesc[]`、`outerId`、`shopId`、`category`/`subCategory`/`thirdCategory`、`subEshopType`。

## 三、电商定向设置（audience 字段释义）

`audience` 是 `Map<String,String>`，值为逗号分隔字符串。

> ⚠️ **必填只有 `interests` 一个键**（2026-11-13 消融实测，商品推广）：不给报 `901216`「兴趣错误」；
> 空对象 `{}` 报 `901214`「定向信息为空」；`sex` 及其余键都可省，服务端会补默认值
> （`region=9999999`、`keywords=""`、`fcKeywordWtpids=""`、`intelligentWeight="0"`…）。
> 另注：`updateCampaignShopping` 的 audience 是**合并语义**，未传的键不会被清空（与 `material` 相反）。
> 详见 [`campaign-shopping.md`](campaign-shopping.md) 第五节。

字段含义与取值：

| 字段 | 含义 | 取值 / 限制 |
|---|---|---|
| `age` | 年龄段 | 0 不限 / 1 <18 / 2 18-24 / 3 25-34 / 4 35-44 / 5 45-54 / 6 >54 / 99 56及以上；可多选逗号分隔；**与 customAge 互斥** |
| `customAge` | 自定义年龄 | `起始,结束`；起始 18-47，结束用 99 表示 56+；0=不限；与 age 互斥 |
| `interests` | 兴趣 | 兴趣 ID（只传最下级），逗号分隔；0=不限 |
| `regionType` | 地域类型 | 0 按省市 / 1 按发展 |
| `region` | 地域 | 地域码逗号分隔；`9999999`=不限（实测为不传时的默认值）。⚠️ **传非法值（如中文「全国」）不报错，会被静默丢弃并按 `9999999`（不限全国）落库** —— 地域定向被放大且回读前看不出，写完必须回读 `region` 校验（2026-11-14 实测） |
| `keywords` | 意图词 | 每个 ≤40 字节，逗号分隔，最多 2000 个，可空 |
| `keywordsExtend` | 意图搜索行为 | 1 全网行为 / 2 历史搜索 |
| `sex` | 性别 | 0 不限 / 1 女 / 2 男 |
| `crowd` | 自定义人群 | 人群 ID 逗号分隔（≤3000 个）；0=不限 |
| `etradecrowds` | 电商推荐人群 | 电商人群 ID 逗号分隔；0=不限（**定向包不支持此字段**） |
| `intelligentWeight` | 智能放量 | 1 使用 / 0 不使用 |
| `excludeTrans` | 排除人群已转化用户 | **0 不限** / 1 同账户 / 4 同客户主体（官方只列 1/4，实测既有计划与新建默认都是 `0`） |
| `excludeCrowd` | 排除人群自定义 | 自定义人群 ID 逗号分隔；0=不限 |
| `fcKeywordWtpids` | 关键词包 | 关键词包 ID **逗号分隔的纯数字**（不能放占位符）；`bindPlanKeywordPackages` 是**替换**语义 |

## 四、账户 `AccountShoppingService`

- `getAccountShopping`（查询账户）：入参空。返回 `userId`、`userStat`（1 未生效/2 生效/3 余额为零/4 被拒/5 预算不足/8 禁止投放/9 产品线未开通）、`budget`（日预算，0=不限）、`balance`、`userName`、`custId`。
- `updateAccountShopping`（更新账户）：入参 `accountShoppingType`，含 `budget`（必填，日预算，0=不限）、`adoptBudget`（必填，是否采用系统最低预算建议，默认 true）。**只支持改账户日预算**。

## 五、电商推荐人群 `CrowdShoppingService.getEshopTradeCrowds`

入参空。返回 `id`、`name`、`groupCount`（覆盖人数）、`children[]`（子人群包）。

## 六、❌ SDK 未覆盖：智投项目 `ProjectShoppingService`

| 方法 | 入参要点 | 说明 |
|---|---|---|
| `getProjectShopping` | `projectShoppingIds`、`sortField`、`isDesc`、`limit`(必填)、`projectShoppingFields`(必填)、`filters` | 查项目 |
| `addProjectShopping` | `projectShoppingTypes[]`：`projectShoppingName`/`targetType`/`budget`/`campaignType`/`audience`/`ideaTypes`/`material` 必填 | 新增项目 |
| `updateProjectShopping` | `projectShoppingTypes[]`：`projectShoppingId` + 上述必填 | 改项目 |
| `deleteProjectShopping` | `projectShoppingIds`(必填) | 删项目（连带预算/创意失效） |

项目返回字段：`projectShoppingId`、`projectShoppingName`、`pause`、`status`、`astat`、`targetType`、`eshopType`、
`productId`、`productName`、`anchorId`、`budget`（项目共享预算）、`transType`、`campaignType`、`ocpcBid`、`roi`、
`startTime`、`endTime`、`schedule`、`audience`、`ideaTypes`（注意是数组）、`material`、`promotionUrl`、
`elementGenerator`、`repayShowStatus`、`repayShowDesc`、`bizType`、`liveStatus`、`liveProductId`、`quickCreate`。

## 七、❌ SDK 未覆盖：定向包 `AtpService`

| 方法 | 入参要点 |
|---|---|
| `getAtpList`（分页列表） | `atpShoppingFields`、`atpShoppingIds`、`sortField`、`sortOrder`、`filters` |
| `getAtps`（列表） | 空 |
| `getAtp`（详情） | `atpId`(必填) |
| `addAtp`（新增） | `atpName`(必填)、`atpDesc`、`audience`(必填) |
| `updateAtp`（更新） | `atpId`(必填)、`atpName`、`atpDesc`、`audience` |
| `deleteAtp`（删除） | `atpIds`(必填) |
| `bindAtp`（关联计划） | `atpBindShoppingTypes[]`：`atpId`(必填)、`bindCampaignIds`(必填) |

返回：`atpId`、`atpName`、`atpDesc`、`relatedCampaignIds[]`（`getAtps` 返回 `atpid`/`atpName`）。
注意：**定向包 `audience` 不支持 `etradecrowds`（电商推荐人群）**。

## 八、❌ SDK 未覆盖：评论管理 `CommentShoppingService`

- `getCommentInfoList`（查询评论列表）：`planIdList`、`planTimeStart`/`planTimeEnd`、`content`、
  `pageIndex`/`pageSize`（首页 0,10）、`ideaIdList`、`replyStatus`（2 已回复且审核拒绝 / 3 未回复）、
  `projectIdList`、`isProject`（false 推广计划 / true 智投）。返回 `threadId`、`replyId`、`planId`、`planName`、
  `ideaId`、`content`、`level`、`likeCount`、`commentTime`、`isTop`、`replyStatus`、`parentReplyId`、`projectId`、`projectName`，总数在 `expand.totalCount`。
- `hideComment`（隐藏评论）：`replyThreadMap`（Map<replyId, threadId>）。

## 九、来源与维护

- 原始官方文档：`tmp/` 25 篇（2026-11-13 抓取，不入库）。
- 字段语义以官方为准；实测偏差记录在 `docs/baidu-behavior.md` 与 `docs/campaign-shopping.md`。
- 新增/改正字段时，同步更新：本文件 + `src/mybmo_mcp/baidu/descriptions.json`（`get_capability_schema` 的 `notes`/`params`）。
