# 电商推广计划（CampaignShoppingService）读写速查

这组接口是"计划级"的读写入口，也是**词包绑定/解绑**真正落地的地方。
下面所有字段表都来自**后台自己的请求**（2026-11-11 抓包）+ 实测。

> 字段含义、取值范围、必填与枚举的**官方口径**见 [`ec-shopping-official.md`](ec-shopping-official.md)。

## 一、读计划：必须用这份完整字段表

`getCampaignShopping` 的 `campaignShoppingFields` **不是随便传几个就行**——漏字段就等于读不到，
而写回时又会把 `material` 整体替换，所以"读不全"会直接变成"写坏"。

**标准字段表（51 个，照抄即可）：**

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
| **`fengchaoDescription` 读不回** | 显式列进 `campaignShoppingFields` 且带 `brand` 也**不返回**（2026-11-13 扫全部 35 个计划均无该键）；读接口取不到，**只能写** |

实测（计划 2079251858）：按上表请求 → 顶层 29 个键、`material` 7 个子字段
（`title 6 / description 6 / pictures 18 / videos 1 / fengchaoTitle 6`，**无 `fengchaoDescription`**——它读不回来，只能写）、
`audience` 14 个键（`keywords` 38 个词、`region` 367 段、`fcKeywordWtpids`）。

## 二、写计划：`material` / `audience` 是整体替换

`updateCampaignShopping` 的 `campaignShoppingTypes[].material` **传什么就是什么**：

- 漏传的子字段 = 被清空（2026-11-11 真实事故：漏读 `fengchaoTitle` → 6 条风巢标题消失）
- 所以**写前必须按第一节的字段表读全，并断言关键子字段非空**

**安全流程：**

```
① getCampaignShopping（用完整字段表）
② 断言：material.title / description / pictures / videos / fengchaoTitle / userPortrait 均非空
③ 只改目标字段（如 audience.fcKeywordWtpids / pictures 追加一张）
④ **把读回来的对象"裁剪"成 schema 允许的字段**（见下）再回传
⑤ 再读一次逐字段比对
```

### ⚠️ 新建（`addCampaignShopping`）：`material.userPortrait` 实测必带

官方文档（`tmp/添加计划`）没给 `userPortrait` 标「必填」，schema 里也没有 `required`，
但**商品推广新建时缺它就是建不出来**（2026-11-13 实测，账户 `baidu-YF电商-玛卡快消01`）：

| 时间 | call_id | material 里有无 userPortrait | 结果 |
|---|---|---|---|
| 15:50 | `f544250ba6ae` | 无 | ❌ `901217`「物料错误：创意设置品牌头像错误」，`field=userPortrait` |
| 15:54 | `63eabbdd872f` | 有 `{imageId:15505964077}` | userPortrait 报错消失，改报 `field=pictures` 尺寸不合规（800×800 商品主图不能当创意图） |
| 16:04 | `f305708a13aa` | 有 | 报 `901216`「兴趣错误」（audience 扁平键不全） |
| 16:08 | `98b453c47249` | 有 | ✅ 成功，`campaignShoppingId 2387079078` |

结论边界：只有**1 个账户 / 1 条商品推广计划（`targetType=1`、`ideaType=1`）/ 1 次成功**的样本，
直播推广与其他账户未验；`901217` 文案是「品牌头像**错误**」，无法区分「缺失」与「不合规」。
所以按「商品推广必须带合法 120×120 品牌头像」处理即可，别写成协议层必填。
参照：官方 `添加智投项目` / `更新智投项目` 文档明确写「userPortrait必填」，计划这条线是官方文档漏标。

### ⚠️ `material.pictures` 尺寸：计划级规则，add / update 共用

`addCampaignShopping` 与 `updateCampaignShopping` 改的是**同一个计划资源**、走同一套后端物料校验，
官方两篇文档对 `pictures` 尺寸的表述也**逐字相同**，所以按一条规则记，不按接口拆。

**官方允许档位**（随推广类型变）：

| 推广类型 | 允许尺寸 | 张数上限 |
|---|---|---|
| 商品推广（`targetType=1`） | **370×245 / 1140×640 / 750×1000** | **≤18 张** |
| 自定义直播推广（`targetType=2`） | 仅 **1140×640** | **≤18 张** |

> 张数：官方两条业务线都写「图片数量小于等于18张」。注意别和字段声明的
> `集合长度限制：[1, 36]` 混淆——36 是 `Picture[]` 的声明上限，18 是实际可用张数。
> **add / update 同一条规则**（同一个计划资源）。
> ⚠️ 18 这个数目前是文档口径 + 「计划 2079251858 恰好 18 张」的观察，**没做 19 张的压测**；
> schema 里 `Material.pictures` 也没有 `maxItems`，参数校验层不会拦第 19 张，别指望它报错。

**实测（2026-11-13，账户 `baidu-YF电商-玛卡快消01`）：**

| 图 | 尺寸 | `uploadImage` | 作为 `pictures` 投放 |
|---|---|---|---|
| `24467907335` | **800×800** | ✅ 成功（call `f516458c9413`） | ❌ `901217`，`field=pictures`（call `63eabbdd872f`） |
| `24468661981` | **750×1000** | ✅ 成功（call `ee26d603cf48`） | ✅ 建计划成功（call `98b453c47249`，计划 2387079078） |

三个要点：

1. **尺寸校验在计划侧，不在上传侧**。`uploadImage` 只返回 `width/height` 不做判定——
   同一张 800×800 图能进图库，投放时才被 `901217` 拒。所以别用「上传成功」当尺寸合规的凭证。
2. **官方这三档不要当穷举白名单**。实测只覆盖「750×1000 通过 / 800×800 被拒」，
   未测转向版本（1000×750、245×370）与 960×540 一类常见尺寸；实际规则可能是
   「指定宽高比（3:2 / 16:9 / 3:4）+ 最小尺寸」。另外 `901217` 文案是「图片不存在**或**尺寸不合规」，
   单次失败不能直接归因到尺寸（本次已排除「不存在」——该图现在图库里查得到）。
3. `updateCampaignShopping` 侧**没有专门的拒绝样本**：那次带 750×1000 的更新报的是
   `901202`「创意类型错误」（material 缺 `iType`/`astat` 一类），在尺寸校验之前就挂了。
   它与 add 共用规则是「同资源 + 同文档表述」的合理默认，不是独立实测。

### ⚠️ 回传前必须裁剪字段

`updateCampaignShopping` 只接受 schema 里声明的字段；而**读回来的对象带一堆只读字段**
（`reAuditStatus` / `auditId` / `smallSize` / `largeSize` / `posterSource` …），
直接原样回传会被参数校验拒绝，报 `xxx 不是该能力的参数`。

各子对象的**可写字段**：

| 子对象 | 可写字段 |
|---|---|
| `material` | `brand` / `userPortrait` / `title` / `fengchaoTitle` / `description` / `pictures` / `videos` / `broadcastCovers` / `ideaId` / `iType` / `astat` |
| `Picture`（含 userPortrait / posters） | `imageId` / `url` / `width` / `height` / `astat` / `elementId` / `refuseReason` |
| `Text`（title / description / fengchaoTitle） | `text` / `astat` / `elementId` / `refuseReason` |
| `Video` | `videoId` / `url` / `width` / `height` / `duration` / `posters` / `astat` / `elementId` / `refuseReason` / `diagnosticInfos` |
| 计划本体 | `campaignShoppingId` / `campaignShoppingName` / `campaignType` / `targetType` / `anchorId` / `budget` / `ocpcBid` / `ideaType` / `transType` / `startTime` / `endTime` / `eshopType` / `productId` / `productName` / `promotionUrl` / `schedule` / `audience` / `material` / `pause` / `status` / `adgroupShoppingId` / `customMaterials` / `diagnosticLabel` / `repayShowDesc` / `repayShowStatus` |

> 实测踩坑：`useLiftBudget` / `liftBudget` / `liftMode` / `aiOptimizer` / `aiFix` / `campaignSource` /
> `intelligentType` / `roi` 这些**读得到但不能写**（不在 schema 里），回传会报"不是该能力的参数"。

### 往计划里加一张图（实测流程）

```
① ImageManageService.uploadImage（addImage=true）→ imageId / url / width / height
② getCampaignShopping（51 字段）
③ material.pictures 追加 {"imageId","url","width","height"}
④ 裁剪字段后 updateCampaignShopping
⑤ 回读：pictures +1，title/description/fengchaoTitle/videos 数量不变
```

> ⚠️ 2026-11-13 一次 update 带 `material.pictures`（750×1000）但 material 里没有 `iType` / `astat` / `ideaId`
> 时报 `901202`「创意类型错误」（call `b48e7a57b79c`，2240B）——**material 回传时把这几个创意类型字段一起带上**，
> 否则会卡在尺寸校验之前。该次之后只用 78B 的 pause 调用（`ff4c9eaccc78`）成功，material 更新路径尚未跑通。

实测（副本 2376038055）：pictures 17 → 18，新增那张 `imageId 24366508906`（1140×640），
`elementId 1615755853062`、`astat 2`；其余素材与暂停状态均未变。

## 三、几个关键字段的含义

| 字段 | 含义 |
|---|---|
| `audience.fcKeywordWtpids` | **关键词包 ID 列表**（逗号分隔，`fc`=风巢）。词包绑定/解绑就改这里 |
| `audience.keywords` | 计划投放的关键词（逗号分隔字符串）。实测 38 个词 |
| `audience.region` / `regionType` | 地域定向（`region` 是逗号分隔的地域码，实测 367 段） |
| `material.fengchaoTitle` | **风巢模板标题**：写入时用 `{关键词}` + 文案槽，按搜索词动态替换；**读回来显示成 `#{文案}`，`#` 就是动态词位的展示形式，不是数据损坏**（实测 6 条） |
| `material.title` / `description` | 普通固定文案（各 6 条） |
| `material.pictures` / `videos` | 图片（18 张，含 370×245 / 750×1000 / 1140×640 三种尺寸）与视频（1 个 + 3 张封面） |
| `material.userPortrait` | 品牌头像（120×120）。官方未标必填，**商品推广新建实测必带**（缺 → `901217` `field=userPortrait`，见第二节） |
| `elementFixStatus` / `liftStatus` / `diagnosticLabel` | 元素修复状态 / 放量状态 / 诊断标签（只读为主） |

## 四、词包绑定 / 解绑

| 动作 | 做法 |
|---|---|
| 绑定 | `KeyWordShoppingService.bindPlanKeywordPackages`：`{bindPlanKeywordTypes:[{packageId, planIds:[...]}]}`，**替换语义** |
| 解绑 | 没有独立接口：按第一节读全 → 从 `audience.fcKeywordWtpids` 移除包 ID → 整体回传 `updateCampaignShopping` |
| 回读确认 | `getKeywordPackages` 的 `bindPlanIdList` / `planCount`，以及计划的 `fcKeywordWtpids` |

> `fengchaoTitle` 的恢复已在后台确认显示正常（2026-11-11）。

实测解绑（计划 2079251858，移除 1615571277581）：
`fcKeywordWtpids` 从 `"1352096810890,1615571277581"` 变为 `"1352096810890"`，
其余字段（38 关键词 / 367 地域 / 6 标题 / 6 描述 / 18 图 / 1 视频）**零变化**。

## 五、错误码

| 码 | 场景 |
|---|---|
| `1519` / `9011519` | 通用「请求字段非法」。⚠️ 但 `campaignShoppingFields` 里放非法字段名**不报此码**，而是被**静默忽略**（2026-11-13 实测 `definitelyNotAField123` 返回 success、`material` 为空）——字段拼错=悄悄读空 |
| `8602` | 缺 `limit`（`getCampaignShopping` 必传 `[offset, size]`）或写接口缺必填字段 |
| `901404` | 词包绑定时 `planIds` 为空 |
| `87091` | 服务名/方法名不存在（解绑没有独立接口，就是这个码） |
