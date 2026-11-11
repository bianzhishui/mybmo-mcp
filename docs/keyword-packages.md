# 电商关键词包（KeyWordShoppingService）

⚠️ **这套接口不在 `baiduads-sdk` 覆盖范围内**，是通过
`scripts/handwritten_capabilities.json` 手写补入能力表的。**没有官方文档**
（`tmp/` 里没有这一组），所以下面的参数结构部分来自实测推断，已在能力说明里标注。

## 一、接口清单与状态

| 能力 | 路径 | 状态 |
|---|---|---|
| `KeyWordShoppingService.getKeywordPackages` | `/json/sms/service/KeyWordShoppingService/getKeywordPackages` | ✅ **读实测通过** |
| `KeyWordShoppingService.addKeywordPackages` | `…/addKeywordPackages` | ◐ 接口存在，**参数结构未确认** |
| `KeyWordShoppingService.updateKeywordPackages` | `…/updateKeywordPackages` | ◐ 同上 |
| `KeyWordShoppingService.delKeywordPackages` | `…/delKeywordPackages` | ◐ 同上（注意是 **del**，不是 delete） |

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

请求参数（实测可用但不完整）：`packageIds`（按包 ID 过滤，未单独验证）、
`keywordPackageFields`（指定返回字段，不传也返回上面这些）。

## 四、写接口的参数结构（**未确认**）

试过的形状都被判 `9015 参数异常`，且**没有 `position` 提示**：

```jsonc
{"keywordPackages": [{}]}                              // 9015
{"keywordPackages": [{"packageName": "probe"}]}        // 9015
{"keywordPackages": [{"packageName": "", "keywordList": []}]}  // 9015
```

说明字段名不是 `keywordPackages`（或还需要别的顶层字段）。**要继续推进，需要以下任一条件：**

1. 官方文档（最理想，字段+枚举一次到位）
2. 后台"关键词包"页面的抓包（浏览器 F12 → Network → 看真实请求体）
3. 逐个顶层字段名试探（成本高、且写接口试探有误建风险，不建议）

在此之前，`addKeywordPackages` / `updateKeywordPackages` / `delKeywordPackages` 在能力表里
**没有 `verified` 标记**，`get_capability_schema` 会提示"未实测，首次调用请小范围试"。

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
