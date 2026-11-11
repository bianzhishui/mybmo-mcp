# 关键词规划师（KRService）实测与接口速查

官方文档（`tmp/` 抓取件，2026-11-11）的提炼 + 真实账户实测结论。原始抓取件不入库。

**状态标记**：✅ 实测通过 / ⚠️ 文档与实测不一致 / ◐ 部分验证 / 未标记 = 只有文档依据

## 一、服务构成

| 官方文档 | 接口 | 本项目能力表 | 实测 |
|---|---|---|---|
| 根据种子词获取拓展词（**实时**） | `KRService.getKRByQuery` | ✅ 有 | ✅ |
| 获取账户主动推荐词 | `KRService.getKRCustom` | ✅ 有 | ✅（返回空） |
| 异步种子词拓词·创建任务 | `KRService.getKRFileIdByWords` | ✅ 有 | ✅ |
| 查询文件状态 | `KRService.getFileStatus` | ✅ 有 | ✅ |
| 获取文件下载 URL | `KRService.getFilePath` | ✅ 有 | ✅ |
| **查询关键词流量** | `PvSearchFunction.getPvSearch` | ❌ **没有** | — |

⚠️ **缺口：`PvSearchFunction.getPvSearch`（查关键词月均搜索量、指导价、竞价激烈程度）不在
`baiduads-sdk` 的覆盖范围里**，所以本项目 389 条能力中**没有这条**。要用只能先扩展能力表
（生成器加手工条目，或另建手写能力）。

## 二、公共参数 `seedFilter`（SeedFilter）

| 字段 | 类型 | 取值 / 限制 | 默认 |
|---|---|---|---|
| `device` | Integer | 0 全部 / 1 计算机 / 2 移动 | 0 |
| `maxNum` | Long | **[1, 1000]**，越大相关性越低 | **300** |
| `negativeWords` | String[] | 长度 [0, 10]，每词 ≤64 字节 | — |
| `positiveWord` | String | 仅一个词，≤64 字节 | — |
| `regionExtend` | Boolean | true = 不推荐未选地域词 | false |
| `removeDuplicate` | Boolean | true = 不推荐账户内已购词 | false |
| `removeCampaignDuplicate` | Boolean | true = 不推荐计划内已购词 | false |
| `searchRegions` | Long[] | 地域代码列表 | 全部地域 |

✅ 实测：`device` / `negativeWords` / `positiveWord` / `removeDuplicate` 都真实生效
（`positiveWord=猫` 返回的全是含"猫"的词）。

## 三、实时拓词 `getKRByQuery`

- `query`：**单个种子词字符串**，≤64 字节。传数组 → `99912 The Json message you provided is invalid` ✅
- 返回条数由 `seedFilter.maxNum` 决定：**不传 300，最大 1000，传 2000 报 `901720`** ✅

返回字段（实测样本比文档更全）：

| 字段 | 说明 | 备注 |
|---|---|---|
| `word` | 推荐词 | |
| `competition` | 竞争程度 1/2/3 | ⚠️ 两份官方文档互相矛盾：`getKRByQuery` 写「1-低 2-中 3-高」，`getKRCustom` 写「1-高 2-中 3-低」 |
| `competitionPc` / `competitionWise` | 计算机 / 移动竞争程度 | |
| `pv` | 月均搜索量 | ⚠️ **文档写 `PV`（大写），实测返回小写 `pv`** |
| `pcPV` / `mobilePV` | 计算机 / 移动月均搜索量 | |
| `show` / `click` | 预估月均展现 / 点击 | |
| `recommendPricePc` / `recommendPriceMobile` | 指导价（元） | |
| `showReasons` | 特色标签，如「高频热搜词」 | |
| `groupName` | 关键词分组名 | |
| `recBid` | 推荐出价 | ⚠️ **文档未列，实测返回** |
| `similar` | 相似度 | ⚠️ 文档未列，实测返回 |
| `wordPackage` | 词包标记 | 实测恒为空串 |
| `businessPoints` | 业务点 | 实测恒为空数组 |

## 四、账户主动推词 `getKRCustom`

| 字段 | 说明 |
|---|---|
| `idType` | **3 = 计划 ID，5 = 单元 ID**（官方枚举，实测只有这两个合法；0/1/2/4/6/7/8 报 `901743`）✅ |
| `id` | 计划或单元 ID；为 null 时 idType 填 3 或 5 均可 ✅（实测 id 省略也返回 `[]`） |

✅ 实测：`idType=3` + 电商计划 ID、`idType=5` + 单元 ID 都返回 `[]` —— 该账户是电商推广线，
没有搜索推广的计划/单元可推词，**接口本身正常**。

## 五、异步拓词（三步）

```
① getKRFileIdByWords({seedWords: [...], seedFilter})      → [{"fileId": "…"}]
② getFileStatus({fileId}) 轮询                            → [{"isGenerated": N}]
③ getFilePath({fileId})                                   → [{"filePath": "https://…csv?authorization=…"}]
```

| 环节 | 实测结论 |
|---|---|
| `seedWords` 条数 | ✅ **实测 1000 个也通过**（文档字段范围 [1,1000] 正确；简介里"单次 100 个"是过时说法） |
| 单任务串行 | ⚠️ **上一个任务没结束就提交新任务 → `901755 Last file task is not finish`**。必须等 `isGenerated=3` 再提交下一个 |
| `isGenerated` | 1 等待中 / 2 处理中 / 3 生成成功 / 4 生成失败 ✅（实测观察到 2 → 3，约 5 秒） |
| `getFilePath` 提前调用 | `901726 The request KR File is still in processing` ✅ |
| 下载地址 | BCE 对象存储 CSV 直链，**签名有效期 3600 秒**（URL 里带 `/3600/`）✅ |

## 六、实测对照（2026-11-11，账户 `baidu-YF电商-玛卡快消01`）

| 项 | 结果 |
|---|---|
| `getKRByQuery` 默认条数 | 300 |
| `maxNum` 500 / 1000 / 2000 | 500 / 1000 / **901720** |
| `query` 传数组 | **99912** |
| `device` / `negativeWords` / `positiveWord` / `removeDuplicate` | 全部生效 |
| `getKRCustom` idType 3 / 5 | 返回 `[]`（无线可推，接口正常） |
| `getKRCustom` idType 0/1/2/4/6/7/8 | **901743** |
| `getKRFileIdByWords` 100 / 200 / 500 / 1000 词 | 全部成功（200 首次失败是因为上一个任务未完成） |
| 文件生成 → 下载 | 2→3 约 5 秒，`filePath` 为 `bj.bcebos.com/v1/papfc/krcsv/*.csv` |

## 七、错误码

| 码 | 文案 | 触发 |
|---|---|---|
| `99912` | `The Json message you provided is invalid.please check the request json` | `query` 传了数组等结构错误 |
| `901720` | `The maximum return number is not valid` | `maxNum > 1000` |
| `901726` | `The request KR File is still in processing` | 文件未生成完就取下载地址 |
| `901743` | `idType is not valid to get KR results` | `getKRCustom` 的 `idType` 不是 3 / 5 |
| `901755` | `Last file task is not finish` | 上一个异步任务未完成就提交新任务 |

## 八、还没确定的

1. **`PvSearchFunction.getPvSearch`（关键词流量）** 不在能力表里 —— 最大缺口，要用得先扩能力表。
2. `competition` 的取值方向（1 是高还是低）两份文档互相矛盾，未用真实数据交叉验证。
3. `searchRegions` 地域码表未取；`regionExtend` / `removeCampaignDuplicate` 未单独验证效果。
4. `similar` / `recBid` 的确切语义文档未给。
