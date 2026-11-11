# 电商关键词包（KeyWordShoppingService）

⚠️ **这套接口不在 `baiduads-sdk` 覆盖范围内**，是通过
`scripts/handwritten_capabilities.json` 手写补入能力表的。**没有官方文档**
（`tmp/` 里没有这一组），所以下面的参数结构部分来自实测推断，已在能力说明里标注。

## 一、接口清单与状态

| 能力 | 路径 | 状态 |
|---|---|---|
| `KeyWordShoppingService.getKeywordPackages` | `…/getKeywordPackages` | ✅ **读实测通过**（汇总视图） |
| `KeyWordShoppingService.getKeyword` | `…/getKeyword` | ✅ **读实测通过**（包内关键词明细） |
| `KeyWordShoppingService.addKeywordPackages` | `…/addKeywordPackages` | ✅ **写实测通过** |
| `KeyWordShoppingService.updateKeywordPackages` | `…/updateKeywordPackages` | ✅ **写实测通过** |
| `KeyWordShoppingService.delKeywordPackages` | `…/delKeywordPackages` | ✅ **写实测通过**（注意是 **del**，不是 delete） |
| `KeyWordShoppingService.delKeyword` | `…/delKeyword` | ✅ **写实测通过**（删包内单个词） |
| `KeyWordShoppingService.downloadKeyword` | `…/downloadKeyword` | ✅ **读实测通过**（导出 CSV） |

## 二、怎么确认一个"SDK 里没有的接口"到底存不存在

实用技巧：**发一个空 body，看错误码**。

| 错误码 | 含义 |
|---|---|
| `87091 The request url is invalid.` | **服务名或方法名写错了**，路径不存在 |
| `9015 参数异常` | **路径是对的**，只是参数不对 → 接口存在 |

实测记录（2026-11-11）：

```
getNothingHere            → 87091   （对照：不存在）
addKeywordPackages        → 9015    （存在）
updateKeywordPackages     → 9015    （存在）
delKeywordPackages        → 9015    （存在）
deleteKeywordPackages     → 87091   （不存在，正确名字是 del）
getKeywordPackageCount    → 87091   （不存在）
getKeywordPackageDetail   → 87091   （不存在）
```

## 三、`getKeywordPackages` 实测结果

不传任何参数即返回该账户全部关键词包：

```
status=0  succ=39  oprs=39  → 39 个关键词包
```

每条的结构（实测字段）：

```json
{
  "packageName": "止咬止啄特效药",
  "packageId": 1339586880767,
  "keywordCount": 112,
  "planCount": 1,
  "bindPlanIdList": [2219484614]
}
```

| 字段 | 说明 |
|---|---|
| `packageId` | 关键词包 ID（后续更新/删除要用） |
| `packageName` | 包名 |
| `keywordCount` | 包内关键词数量 |
| `planCount` | 绑定的计划数量 |
| `bindPlanIdList` | 绑定的计划 ID 列表 |

真实请求参数（后台抓包）：

```jsonc
{"sortField": "packageName", "sortOrder": "asc",
 "pageNo": 1, "pageSize": 20,
 "filters": [{"field": "packageName", "op": "like", "values": ["测试用"]}],
 "keywordPackageFields": ["keywordDetail"]}
```

| 参数 | 实测 |
|---|---|
| `pageNo` / `pageSize` | ✅ 生效（`pageSize=5`：第 1/2 页各 5 条、第 8 页 3 条） |
| `filters` | ✅ 生效，形状是 **`{field, op, values:[...]}`**（不是 `operatorValue`/`value`）。`op:"like"` + `values:["特效药"]` → 39 个包里命中 2 个 |
| `sortField` / `sortOrder` | ✅ 生效（`keywordCount` + `desc` → 500 词那个包排第一） |
| `expand.totalCount` | ✅ 返回总数（39），配合分页判断是否到底 |
| `keywordPackageFields: ["keywordDetail"]` | ❌ **无效果**，返回仍是汇总字段 → 包内具体词请用 `getKeyword` |

> 之前我用 `{field, operatorValue, value}` 猜的形状全部返回 0 条，就是这个原因——
> 后台传的是 `op` + `values` 数组。又一个"不能猜、要抓包"的例子。

## 三点五、包内关键词明细 `getKeyword`

后台抓包得到的参数形状：

```jsonc
{"packageIds": [1615571277581], "statusList": [0, 1, 2]}
```

| 参数 | 说明 |
|---|---|
| `packageIds` | **必填**，可一次传多个包（空 body → `9015 参数异常`） |
| `statusList` | 按词状态过滤；不传返回该包全部词 |

返回：

```jsonc
[{"packageName": "测试用02", "packageId": 1615571277581,
  "keywordTypes": [
    {"index": 1, "keyword": "黄金测试", "wordId": 247465450,
     "wordDataId": 1615571277582, "status": 2, "statusDesc": "审核中"}]}]
```

| 字段 | 说明 |
|---|---|
| `index` | 序号 |
| `keyword` | 关键词字面 |
| `wordId` | 词 ID |
| `wordDataId` | 包内词数据 ID |
| `status` / `statusDesc` | 词状态：实测 **0=有效、2=审核中**；1/3 等未观测到 |

实测：一次查 3 个包返回 **699 个词**（全部 `status=0 有效`）；新包只有 1 个词且 `status=2 审核中`；
`statusList=[0]` 与 `[2]` 分别过滤生效。

> 与 `getKeywordPackages` 的分工：**汇总用 getKeywordPackages**（包名/词数/绑定计划 + 分页过滤），
> **看包里到底有哪些词用 getKeyword**。

## 三点六、导出关键词包 `downloadKeyword`

```jsonc
{"packageIds": [1615622617439]}
```

返回（实测是 **data 数组**；后台页面拿到过对象形状，解析时两种都兼容）：

```jsonc
{"data": [{"downloadUrl": "https://papfeed.cdn.bcebos.com/webresource/portrait/20261111/…/闭环电商平台关键词信息+20261111+baidu-YF电商-玛卡快消01.csv"}]}
```

| 要点 | 说明 |
|---|---|
| 参数 | 只有 `packageIds`（必填，可多个包）；空 body → `9015` |
| 下载地址 | bcebos CDN **直链、无需鉴权**；URL 含中文与空格，请求前要 **URL 编码** |
| 文件编码 | ⚠️ **GB18030 / GBK**（按 UTF-8 解码会乱码） |
| 列 | `词包名称, 关键词, 审核状态` |
| 内容 | 即时快照；包内词变了要重新导出 |

实测：3 词包 → `succ=1` → 下载 HTTP 200、121 字节、4 行（表头 + 3 个词，均"审核中"）。

## 四、写接口：字段名从后台抓包得到（已实测）

后台控制台的请求体里，业务参数是这一段（URL 解码后）：

```json
{"keywordPackageTypes": [
  {"packageName": "测试用",
   "keywordTypes": [{"keyword": "麻将检测", "id": 0}]}
]}
```

于是三个结论立刻确定：**顶层字段是 `keywordPackageTypes`**（不是 `keywordPackages`）、
**包内词是 `keywordTypes: [{keyword, id}]`**、**新建时 `id` 填 0**。

### `addKeywordPackages` ✅ 两种模式

**① 新建**：给包名 + 关键词

```jsonc
{"keywordPackageTypes": [
  {"packageName": "mcp-probe-1111",
   "keywordTypes": [{"keyword": "测试词一", "id": 0}, {"keyword": "测试词二", "id": 0}]}]}
```

**② 复制**（页面上的「复制」按钮就是这个）：只给 `packageName` + **源包 `packageId`**，不带 `keywordTypes`

```jsonc
{"keywordPackageTypes": [{"packageName": "测试用02_副本", "packageId": 1615571277581}]}
```

实测：源包 3 个词 → 副本 3 个词完全一致、`packageId` 不同、`succ=1`。
**注意**：复制模式下 `keywordTypes` 会被忽略（不要以为传了就能"复制+改词"）。

### 原 `addKeywordPackages` 实测记录

```jsonc
{"keywordPackageTypes": [
  {"packageName": "mcp-probe-1111",
   "keywordTypes": [{"keyword": "测试词一", "id": 0}, {"keyword": "测试词二", "id": 0}]}]}
```

实测：`status=0 succ=1`，返回 `packageId=1615561300505`；`getKeywordPackages` 回读包数 **39 → 40**。

### `updateKeywordPackages` ✅

同结构 + 必须带 `packageId`：

```jsonc
{"keywordPackageTypes": [
  {"packageId": 1615561300505, "packageName": "mcp-probe-1111-renamed",
   "keywordTypes": [{"keyword": "测试词三", "id": 0}]}]}
```

实测 `succ=1`，响应回显新包名；⚠️ `keywordTypes` 是**整组替换**，不是追加。

### `delKeyword` ✅ 删包内单个词

```jsonc
{"delKeyWordDataIdList": [1615615630767]}
```

| 要点 | 说明 |
|---|---|
| 字段名 | **`delKeyWordDataIdList`**（`KeyWord` 的 W 大写） |
| ID 来源 | `getKeyword` 返回的 `keywordTypes[].wordDataId`（不是 `wordId`、也不是词面） |
| 实测 | 2 个词的探测包删掉「词A」→ 回读只剩「词B」，`succ=1` |

### `delKeywordPackages` ✅

后台抓包得到的形状：

```jsonc
{"delKeywordPackageIds": [1615547902470], "deleteAll": false, "excludeIds": []}
```

实测：删掉探测包 → `succ=1`，响应回显 `delKeywordPackageIds`，回读包数 39 → 38。

`deleteAll=true` 时按 `excludeIds` 排除；日常用 `false` + `delKeywordPackageIds` 精确删除。

> 教训：字段名靠猜是猜不出来的——`keywordPackageTypes` / `delKeywordPackageIds` 都不是直觉名字，
> 而错误码只说「参数异常」不给 position。**有后台抓包就别猜。**

## 五、与其他"包"的区别（容易混）

| 名字 | 服务 | 是什么 |
|---|---|---|
| **关键词包** | `KeyWordShoppingService` | 电商推广的词包，可绑计划（本文件） |
| OCPC 定向包 | `OcpcService.*TargetPackage` | 搜索推广 OCPC 的定向包，增删改查齐全 |
| KR 词表文件 | `KRService.getKRFileIdByWords` 等 | 拓词产出的 CSV 文件，不是可维护的实体 |
| 应用包 | `AppCenterPackageService` | 应用中心的应用包，与投放无关 |

## 六、下一步

1. 拿到文档/抓包后，回填 `scripts/handwritten_capabilities.json` 里
   `KWShopPackageAddRequest` / `Update` / `Del` 三个 schema，以及
   `descriptions.json` 对应条目的 `params`，并给它们打 `verified`。
2. `getKeywordPackages` 可以补验：`packageIds` 过滤、`keywordPackageFields` 指定字段。
