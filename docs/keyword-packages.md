# 电商关键词包（KeyWordShoppingService）

⚠️ **这套接口不在 `baiduads-sdk` 覆盖范围内**，是通过
`scripts/handwritten_capabilities.json` 手写补入能力表的。**没有官方文档**
（`tmp/` 里没有这一组），所以下面的参数结构部分来自实测推断，已在能力说明里标注。

## 一、接口清单与状态

| 能力 | 路径 | 状态 |
|---|---|---|
| `KeyWordShoppingService.getKeywordPackages` | `/json/sms/service/KeyWordShoppingService/getKeywordPackages` | ✅ **读实测通过** |
| `KeyWordShoppingService.addKeywordPackages` | `…/addKeywordPackages` | ✅ **写实测通过** |
| `KeyWordShoppingService.updateKeywordPackages` | `…/updateKeywordPackages` | ✅ **写实测通过** |
| `KeyWordShoppingService.delKeywordPackages` | `…/delKeywordPackages` | ✅ **写实测通过**（注意是 **del**，不是 delete） |

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

**分页实测可用**：`pageNo` + `pageSize`（从 1 开始）。`pageSize=5` 时第 1、2 页各 5 条，
第 8 页 3 条 → 该账户共 **38 个包**。

⚠️ **过滤没打通**：照后台 URL 的 `filters=[{field, operatorValue, value}]` 形状传，
`like` / `LIKE` / `contains` / `eq` / 按 `packageId` 全都返回 **0 条**。要按包名筛，暂时先翻页自己过滤。

想看包内具体词，目前没有对应接口（`getKeywordPackageDetail` 报 87091）。

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

### `addKeywordPackages` ✅

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
