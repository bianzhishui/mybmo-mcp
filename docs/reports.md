# 推广报告速查（OpenApiReportService）

百度营销开放平台的报表只有一个入口：`OpenApiReportService`，三个方法——同步取数、建异步任务、查任务状态。
本文是 `tmp/` 里 12 篇官方文档（2026-11-11 抓取）的提炼版；原始抓取件不入库（见 `.gitignore` 的 `tmp/`）。

**标记约定**

| 标记 | 含义 |
|---|---|
| ✅ | 已用真实账户（`baidu-YF电商-玛卡快消01`）实测验证 |
| ⚠️ | 实测与官方文档不一致，或文档没写但会影响正确性 |
| 无标记 | 按官方文档记录，**尚未实测**，不得当成结论引用 |

## 一、三个方法

| 方法 | 作用 | 端点 |
|---|---|---|
| `getReportData` | 同步取数，支持分页 | `/json/sms/service/OpenApiReportService/getReportData` |
| `createReportTask` | 建异步导出任务，返回 `taskId` | `…/createReportTask` |
| `getTaskStatus` | 轮询任务状态，成功后给下载地址 | `…/getTaskStatus` |

在本项目里它们是三条能力，走 `execute_capability` 调用；`createReportTask` 已在
[`baidu/descriptions.json`](../src/mybmo_mcp/baidu/descriptions.json) 里声明 `async_task`，
所以支持 `execute_capability(..., wait=True)` 自动轮询到终态。

## 二、通用参数

| 参数 | 类型 | 说明 |
|---|---|---|
| `reportType` | Integer | **必填**，报告唯一标识，见[第四节](#四reporttype-全表) |
| `userIds` | Long[] | 查询超管所辖的多个账户；可为空，默认只查当前账户 |
| `timeUnit` | String | `HOUR` / `DAY` / `WEEK` / `MONTH` / `SUMMARY`（**大写**） |
| `startDate` / `endDate` | Date | `yyyy-MM-dd` |
| `columns` | String[] | **必填**，且**至少要带一个转化指标**；每个报告支持的列见各报告文档 |
| `sorts` | Sort[] | `{column, sortRule: ASC\|DESC}`，最多 2 条，排序列必须在 `columns` 里 |
| `filters` | Filter[] | `{column, operator, values}`，见下 |
| `needSum` | Boolean | 是否要总计，默认不要 |
| `startRow` / `rowCount` | Integer | 分页，**仅同步接口有效**（异步忽略这两个参数） |

**过滤操作符**：`GT` / `GTE` / `LT` / `LTE` / `EQ` / `NOT_EQ` / `IN` / `NOT_IN`
- `GT/GTE/LT/LTE` 只能用于数字列；
- 除 `IN`/`NOT_IN` 外只接受**一个值**；`IN`/`NOT_IN` 最多 **500** 个值；
- Enum 列用 **key** 过滤（如 `campaignType: "1"` = 商品）；
- 查某些计划的数据：`{"column":"campaignId","operator":"IN","values":["92210973"]}`。

**时间与容量限制**

| 约束 | 值 | 依据 |
|---|---|---|
| 最大时间区间 | **824 天** | ✅ 超了报 `901606 时间区间不能超过824天!` |
| 日期格式 | `DAY: 2021-03-31`；`HOUR: 2021-03-31 23:00`；`WEEK/MONTH/SUMMARY` 为该周期的汇总 | 文档 |
| 一站式多渠道报告不支持 `cpm` | 文档 2024-05-29 更新说明 | 文档 |

## 三、同步 vs 异步

**同步（`getReportData`）**：直接返回行数据，用 `startRow`/`rowCount` 分页。

**异步（`createReportTask` → `getTaskStatus`）**：

```
createReportTask  {reportType, userIds, startDate, endDate, timeUnit, columns, sorts, filters, needSum}
  → { "data": [ { "taskId": "…" } ] }              taskId 有效期 10 分钟
getTaskStatus     { taskId }
  → { "data": [ { "taskStatus": "SUCCESS", "fileUrl": "…", "dataStartRow": 2, "tableHeader": [...] } ] }
```

| 字段 | 说明 |
|---|---|
| `taskStatus` | `SUBMITTED` / `RUNNING` / `SUCCESS` / `FAIL`（✅ 实测只见到 `RUNNING`→`SUCCESS`） |
| `submitTime` / `startTime` / `finishTime` | 时间戳字符串 `2026-11-11 10:51:25.184` |
| `fileUrl` | **仅 SUCCESS 时有值**，地址有效期 **5 分钟** |
| `dataStartRow` | 数据在文件中的起始行（前面几行是表头/其他信息） |
| `tableHeader` | 每列对应的字段名数组 |

文件解析三条硬要求（文档原文意思）：
1. **不保证起始行与列顺序稳定**，必须按 `dataStartRow` + `tableHeader` 动态解析；
2. 分隔符是**制表符 `\t`**；
3. 生成耗时「一般 1 分钟内，最长 10 分钟」（✅ 实测我们这次 ~0.7 秒）。

⚠️ **两个接口的响应都是单元素数组**：`taskId` / `taskStatus` / `fileUrl` 都在 `[0]` 里。
取字段时先展开数组，否则 `wait=True` 会拿不到 `taskId` 而静默退化成"无法轮询"。

## 四、reportType 全表

### 一站式多渠道（账户粒度，跨搜索/信息流/阿拉丁/知识营销）

| reportType | 报告 | 状态 |
|---|---|---|
| **1783967** | 一站式多渠道账户报告（列含 `product` 区分渠道：0 搜索、1 信息流、3 阿拉丁、4 知识营销） | ✅ 实测跑通 |

### 电商推广（600xxx）

**以下 13 个 reportType 已用真实账户逐个跑通**（2026-11-11，2026-11-07~08，`timeUnit=DAY`）。
「计划维度」那几个用 `filters: [{column: campaignId, operator: IN, values: [...]}]` 收窄到单计划验证。

| reportType | 报告 | 实测 | 备注 |
|---|---|---|---|
| **600001** | 电商账户报告 | ✅ 2 行 | |
| **600002** | 电商计划报告 | ✅ 48 行 | `campaignType` 返回 **value**（"商品"），不是 key |
| **600037** | 电商商品报告·全部 | ✅ 48 行 | `ProductName` 首字母大写（文档如此） |
| **600004** | 电商商品报告·计划维度 | ✅ 2 行 | 需 `filters` 指定 `campaignId` |
| **600003** | 电商直播间报告·计划维度 | ✅ 0 行 | 账户无直播间数据，**枚举本身有效**（不报错） |
| **600033** | 电商直播间报告·全部 | ✅ 0 行 | 同上 |
| **600014** | 电商智投报告 | ✅ 0 行 | 账户无智投项目，枚举有效 |
| **600031** | 电商创意视频报告·全部 | ✅ 1 行 | `videoDuration` 是**字符串**（"3分48秒"） |
| **600012** | 电商创意视频报告·计划维度 | ✅ 0 行 | 枚举有效 |
| **600027** | 电商创意组合报告·全部 | ✅ 1075 行 | `combInfo.combContent` 是**JSON 字符串**，需二次解析 |
| **600010** | 电商创意组合报告·计划维度 | ✅ 10 行 | 同上 |
| **600029** | 电商创意元素报告·全部 | ✅ 2149 行 | `elementType`（"标题"）、`imageId/videoId` 为 0 时名称是 `"-"` |
| **600011** | 电商创意元素报告·计划维度 | ✅ 20 行 | 同上 |

其余几个（600028 / 600030 / 600032 / 600034 / 600038，均为智投项目维度）**未实测**，需要传
`projectShoppingId` / `projectId`，当前账户没有智投项目。

| reportType | 报告 | 备注 |
|---|---|---|
| 600028 | 电商创意组合报告·智投项目维度 | 未实测 |
| 600030 | 电商创意元素报告·智投项目维度 | 未实测 |
| 600032 | 电商创意视频报告·智投项目维度 | 未实测 |
| 600034 | 电商直播间报告·智投项目维度 | 未实测 |
| 600038 | 电商商品报告·智投项目维度 | 未实测 |

> 「计划维度」= 传 `campaignId`；「智投项目维度」= 传 `projectShoppingId`/`projectId`。
> 以上均**未实测**，列名以各报告文档为准。

### 搜索推广

| reportType | 报告 | | reportType | 报告 |
|---|---|---|---|---|
| 2208157 | 账户报告 | | 2665235 | 创意报告 |
| 2290316 | 计划报告 | | 6438125 | 创意组件报告 |
| 2284618 | 单元报告 | | 1150477 | 高级样式报告 |
| 2602783 | 关键词报告 | | 144322 | 商品组件报告 |
| 2307838 | 搜索词报告 | | 962454 | 图片组件报告 |
| 248654 | 商品报告 | | 1025056 | 视频报告 |
| 9718404 | 人群定向报告 | | 5381679 | 落地页报告 |
| 4017932 | 访客明细报告 | | 970638 / 999241 | OPCV 报告（账户 / 计划） |
| 1237122 | 实时报告-关键词实时排名 | | 1212048 | 实时报告-推广计划实时消费 |
| 107181 / 315876 | 无效点击-IP 计费 / 实时计费 | | 2496806 / 2482197 | 小程序报告（搜索词 / 关键词） |
| 5932678 | 门店报告 | | | |

### 信息流推广

| reportType | 报告 | | reportType | 报告 |
|---|---|---|---|---|
| 2149145 | 账户报告 | | 2371731 | 定向分析-自动扩量 |
| 2276038 | 计划报告 | | 2094816 | 创意报告 |
| 2330652 | 单元报告 | | 2347699 / 46914 | 程序化创意报告（整体 / 元素数据） |
| 2324048 | 定向-地域 | | 114718 | 视频报告 |
| 2503330 | 定向-性别 | | 6098145 | 落地页报告 |
| 2510393 | 定向-年龄 | | 6759418 | 访客明细报告 |
| 2521394 | 定向-新兴趣 | | 2650733 | 小程序报告 |
| 2734302 | 定向-老兴趣 | | 910207 / 2437984 / 2446877 | 百青藤（媒体 ID / 分类 / 媒体包） |
| 2532512 | 定向-意图词 / 平台 / 网络 | | 3905781 | 门店报告 |
| 9718403 / 9718402 | 定向-人群 / 自动定向 | | 2340654 | 直播间报告 |
| 6028974 | 定向-地域_区县 | | 2542584 | 定向-学历 |
| 2558533 | 定向-文章类型 | | 2583299 / 2672618 | 定向-商圈 / 场所 |
| 2729420 | 定向-人生阶段 | | 2705779 / 2713436 | 定向-APP 分类（/ 名称） |

⚠️ 官方表里 `2532512` 同时标给「定向分析-意图词」「平台」「网络」三个报告，明显是文档笔误，实际取值待确认。

## 五、列字典

### 通用属性列

`date`（日期）、`userId`（账户 ID）、`userName`（账户中文名）、`product`（渠道，仅一站式）、
以及各报告的物料维度列：`campaignId` / `campaignNameStatus`（名称，已删物料带 `[已删除]` 后缀）/
`campaignType` / `storeType` / `productId` / `ProductName` / `projectShoppingId` / `projectNameStatus` /
`anchorId` / `anchorName` / `liveRoomId` / `liveRoomName` / `liveRoomURL` /
`videoId` / `videoName` / `combId` / `elementId` / `elementType`。

### 效果指标

| 列 | 说明 |
|---|---|
| `impression` | 展现 |
| `click` | 点击 |
| `cost` | 消费 |
| `ctr` | 点击率 |
| `cpc` | 平均点击价格 |
| `cpm` | 千次展现消费（一站式多渠道报告**不支持**） |

### 电商转化指标 `dsOcpcConversionsDetail<NN>`

同一套编码在账户/计划/商品/直播间/视频/创意报告里复用，后缀决定口径：

| 后缀 | 含义 |
|---|---|
| `dsOcpcConversionsDetail<NN>` | 转化数 |
| `…Cost` | 转化成本 |
| `…CVR` | 转化率 |
| `…Amount` | 转化金额 |
| `…AmountROI` | 转化 ROI |

`<NN>` 编码表（来自电商账户/计划报告）：

| NN | 转化 | NN | 转化 |
|---|---|---|---|
| 14 | 订单提交成功 | 83 | 直播间商品按钮点击 |
| 80 | 直播间成单 | 84 | 直播间停留 |
| 81 | 直播间关注 | 85 | 直播间评论 |
| 82 | 直播间观看 | 86 | 直播间打赏 |
| `82Uv` | 直播间观看**人数** | 87 | 直播间购物袋点击 |
| 90 | 商品支付成功 | 88 | 直播间加入购物车 |

智投报告里另有 `ct` 前缀（如 `ctDsOcpcConversionsDetail14`）与 `46` / `48` 编码，含义文档未列表说明，**未实测**。

## 六、实测对照（2026-11-11，真实账户）

| 项 | 结果 |
|---|---|
| `getReportData` + `reportType=1783967`、`timeUnit=DAY` | ✅ 正常返回，默认列 `date/userName/impression/cost/ctr/cpc` |
| 分页 | ✅ `startRow=5,rowCount=5` 换页成功；`rowCount`（本次）/`totalRowCount`（总数）都返回 |
| `rowCount=10000` 拉 30 天 | ✅ 只回 30 行 → 受 `totalRowCount` 约束，**真实单次上限未触顶** |
| 区间 2024-01-01~2026-11-09 | ✅ 报 `901606 时间区间不能超过824天!` |
| `timeUnit=day`（小写） | ✅ 报 `1519 Request field is invalid` → **必须大写** |
| `createReportTask` → `getTaskStatus` | ✅ `RUNNING` → `SUCCESS`，终态直接带 `fileUrl` / `tableHeader` / `dataStartRow`，全程 ~0.7 秒 |
| 电商 600001 / 600002 / 600037 / 600004 / 600003 / 600033 / 600014 / 600031 / 600012 / 600027 / 600010 / 600029 / 600011 | ✅ 13 个 reportType 全部可用（0 行也表示枚举有效，不报错） |
| `filters` + `campaignId IN` | ✅ 生效：600004/600010/600011 用它收窄到单计划 |
| Enum 列（`campaignType` / `elementType` / `combOpenFlagEnum`） | ✅ 过滤时用 key，**返回的是 value**（"商品" / "标题" / "客户自提"） |
| `execute_capability(..., wait=True)` | ✅ `polls=2`、`timed_out=false`，返回终态对象 |
| ⚠️ 默认排序 | 相邻两页日期不连续（10-15…10-22 接着 10-27…10-25）→ **取全量必须显式传 `sorts`** |
| ⚠️ 响应外层 | 网关真实响应是 `{header:{status,desc,succ,oprs,failures,quota,rquota,traceid}, body:{data,expand}}`；文档里的 `{status,data,success}` 是调试页的包装，不是网关原文 |

样本见 [`tests/fixtures/report.json`](../tests/fixtures/report.json) 与
[`tests/fixtures/report_task.json`](../tests/fixtures/report_task.json)。

## 七、在本项目里怎么调

```jsonc
// execute_capability
{
  "capability": "OpenApiReportService.getReportData",
  "account": "main",
  "input": {
    "reportType": 1783967,
    "startDate": "2026-11-07",
    "endDate": "2026-11-09",
    "timeUnit": "DAY",
    "sorts": [{"column": "date", "sortRule": "ASC"}],   // 取全量时必传
    "startRow": 0,
    "rowCount": 200
  }
}
```

异步导出只要加 `"wait": true`，服务端会轮询到 `taskStatus=SUCCESS` 并把带 `fileUrl` 的终态返回；
超时返回 `{timed_out: true}` 而不是报错（任务已建，重试没有意义）。

## 八、还没验证的（别当成结论用）

1. **电商 600028 / 600030 / 600032 / 600034 / 600038**（智投项目维度）：当前账户无智投项目，未验。
2. **搜索推广 / 信息流的全部 reportType**：**范围外**——本项目只做电商推广线，取值仅作参考，不对外承诺。
3. **单次返回行数上限**：`rowCount` 传 10000 没触顶，真实上限未知。
4. `HOUR` / `WEEK` / `MONTH` 三种时间单位的实际行为（只验了 `DAY` 与 `SUMMARY`）。
5. `needSum`、`withColumnMeta`（只验过 `withColumnMeta` 返回列中文名）、`filters` 各操作符的实际效果。
6. 异步任务的 `FAIL` 状态长什么样、失败时 `fileUrl` 是否为空。
