# 电商推广计划（CampaignShoppingService）读写速查

这组接口是"计划级"的读写入口，也是**词包绑定/解绑**真正落地的地方。
下面所有字段表都来自**后台自己的请求**（2026-11-11 抓包）+ 实测。

## 一、读计划：必须用这份完整字段表

`getCampaignShopping` 的 `campaignShoppingFields` **不是随便传几个就行**——漏字段就等于读不到，
而写回时又会把 `material` 整体替换，所以"读不全"会直接变成"写坏"。

**标准字段表（52 个，照抄即可）：**

```json
["targetType","anchorId","intelligentType","campaignShoppingName","ideaType","campaignType",
 "budget","ocpcBid","roi","startTime","endTime","eshopType","excludeCrowd","excludeTrans",
 "productName","productId","schedule","sex","age","fcKeywordWtpids","transType","customAge",
 "regionType","region","interests","keywords","keywordsExtend","crowd","etradecrowds","brand",
 "userPortrait","title","fengchaoTitle","description","pictures","videos","broadcastCovers",
 "customMaterials","intelligentWeight","diagnosticLabel","status","pause","liftBudget",
 "liftMode","useLiftBudget","aiOptimizer","liftStatus","campaignSource","atpShoppingId",
 "picAndVideoName","elementFixStatus"]
```

### 两个容易踩的组装规则（实测）

| 规则 | 说明 |
|---|---|
| **`brand` 是 material 的开关** | 请求里不带 `brand`，返回的 `material` **整个为空**；只请求 `fengchaoTitle` 也一样拿不到 |
| **`fengchaoTitle` 要显式列** | 不列进字段表就不返回——它不会因为请求了 `title` 就附带出来 |

实测（计划 2079251858）：按上表请求 → 顶层 29 个键、`material` 7 个子字段
（`title 6 / description 6 / pictures 18 / videos 1 / fengchaoTitle 6`）、
`audience` 14 个键（`keywords` 38 个词、`region` 367 段、`fcKeywordWtpids`）。

## 二、写计划：`material` / `audience` 是整体替换

`updateCampaignShopping` 的 `campaignShoppingTypes[].material` **传什么就是什么**：

- 漏传的子字段 = 被清空（2026-11-11 真实事故：漏读 `fengchaoTitle` → 6 条风巢标题消失）
- 所以**写前必须按第一节的字段表读全，并断言关键子字段非空**

**安全流程：**

```
① getCampaignShopping（用完整字段表）
② 断言：material.title / description / pictures / videos / fengchaoTitle 均非空
③ 只改目标字段（如 audience.fcKeywordWtpids）
④ updateCampaignShopping 回传完整对象
⑤ 再读一次逐字段比对；对比不到的（创意审核态等）让用户在后台确认
```

## 三、几个关键字段的含义

| 字段 | 含义 |
|---|---|
| `audience.fcKeywordWtpids` | **关键词包 ID 列表**（逗号分隔，`fc`=风巢）。词包绑定/解绑就改这里 |
| `audience.keywords` | 计划投放的关键词（逗号分隔字符串）。实测 38 个词 |
| `audience.region` / `regionType` | 地域定向（`region` 是逗号分隔的地域码，实测 367 段） |
| `material.fengchaoTitle` | **风巢模板标题**：`{关键词}` + 文案槽，按搜索词动态替换（实测 6 条） |
| `material.title` / `description` | 普通固定文案（各 6 条） |
| `material.pictures` / `videos` | 图片（18 张，含 370×245 / 750×1000 / 1140×640 三种尺寸）与视频（1 个 + 3 张封面） |
| `material.userPortrait` | 品牌头像（120×120） |
| `elementFixStatus` / `liftStatus` / `diagnosticLabel` | 元素修复状态 / 放量状态 / 诊断标签（只读为主） |

## 四、词包绑定 / 解绑

| 动作 | 做法 |
|---|---|
| 绑定 | `KeyWordShoppingService.bindPlanKeywordPackages`：`{bindPlanKeywordTypes:[{packageId, planIds:[...]}]}`，**替换语义** |
| 解绑 | 没有独立接口：按第一节读全 → 从 `audience.fcKeywordWtpids` 移除包 ID → 整体回传 `updateCampaignShopping` |
| 回读确认 | `getKeywordPackages` 的 `bindPlanIdList` / `planCount`，以及计划的 `fcKeywordWtpids` |

实测解绑（计划 2079251858，移除 1615571277581）：
`fcKeywordWtpids` 从 `"1352096810890,1615571277581"` 变为 `"1352096810890"`，
其余字段（38 关键词 / 367 地域 / 6 标题 / 6 描述 / 18 图 / 1 视频）**零变化**。

## 五、错误码

| 码 | 场景 |
|---|---|
| `1519` / `9011519` | `campaignShoppingFields` 里有非法字段名 |
| `8602` | 缺 `limit`（`getCampaignShopping` 必传 `[offset, size]`）或写接口缺必填字段 |
| `901404` | 词包绑定时 `planIds` 为空 |
| `87091` | 服务名/方法名不存在（解绑没有独立接口，就是这个码） |
