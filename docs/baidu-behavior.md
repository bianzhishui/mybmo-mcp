# 百度接口行为实测

只记**结论与依据**，不记尝试过程。验证方法见
[`openspec/changes/live-validation/design.md`](../openspec/changes/live-validation/design.md)。

**状态标记**：

- `已验证` —— 必须附依据（响应片段或命令）
- `已知未知` —— 明确知道没验、也知道**为什么没验**（缺账户/缺接口/缺文档），集中列在第四节
- `范围外` —— 本项目不覆盖的推广线（只做电商推广），不再追

## 一、身份与令牌

| 问题 | 结论 | 依据 |
|---|---|---|
| `userName` 取什么 | **已验证：令牌所属账户的中文名**（本账户为 `baidu-YF电商-玛卡快消01`），不是登录名 | 2026-11-11 绑定后 `list_accounts` 返回该值，且用它调 `CampaignService.getCampaign` / `KRService.getKRByQuery` 均成功 |
| 回调带回哪些参数 | **已验证：带回授权码、`state` 与 `userId`**（`userId` 有值才可能换到令牌，`userId=0` 时百度报 `code=3`） | 2026-11-11 真实授权一次，凭据以 state 里的别名 `main` 落地、`status=active`；字段名仍按多候选兼容 |
| 81020 的触发条件 | **已验证：请求头 `userName` 不是令牌所属账户中文名**。返回 `code=81020, position=_user, message="The username information in the request is incorrect."`，`header.status=2`、`succ=0` | 2026-11-11 用 `userName=绝对不存在的账户名` 调 `AccountService.getAccountInfo` |
| 自定义 `state` 是否原样回传 | **已验证：原样回传**（绑定按 state 里的别名落库，未退化成 `acct_<userId>`） | 2026-11-11 真实授权一次，`/data/accounts/main.json` 生成、`binding_state/` 被消费 |
| `refreshToken` 刷新后是否立即轮换 | **已知未知**：刷新本身可用（Casdoor 侧实测能换到新令牌），但「旧 refresh 是否立即失效」未验 | — |
| `accessToken` 有效期 | **已验证：1 天**（`expiresIn=86400`），`refreshToken` 30 天。与「文档称 30 天」不符 | 2026-11-11 落库凭据：access 剩 1.0 天、refresh 剩 30.0 天 |
| 应用级凭据能否跨账户使用 | **已知未知**：只有一个账户可测；推断同一 AppID 下多账户各自授权 | — |

## 二、分页与上限

| 问题 | 结论 | 依据 |
|---|---|---|
| 报表 Enum 列的返回形态 | **过滤用 key，返回 value**（`campaignType` 传 "1" 返回"商品"） | 2026-11-11 电商 600002 / 600029 实测 |
| 报表 `filters` | **生效**：`{column: campaignId, operator: IN, values: [...]}` 可把报告收窄到单计划 | 2026-11-11 600004 / 600010 / 600011 实测 |
| 报表时间区间上限 | **824 天**，超出报 `901606 时间区间不能超过824天!` | 2026-11-11 传 2024-01-01~2026-11-09 实测 |
| 报表单次返回行数上限 | **未触顶**：`rowCount=10000` 请求 30 天数据只回 30 行（受 `totalRowCount` 约束），真实上限仍未测到 | 2026-11-11 一站式多渠道报告实测 |
| 报表是否支持分页（字段名） | **已验证**：请求 `startRow` + `rowCount`，响应 `rowCount`（本次行数）/ `totalRowCount`（总行数）。**默认排序不稳定**（相邻两页日期不连续），取全量必须先传 `sorts` | 2026-11-11 30 天 DAY 实测：`rowCount=5` → 5 行/总数 30，`startRow=5` → 换了一页 |
| `getKRByQuery` 返回条数 | **已验证：由 `seedFilter.maxNum` 控制——不传默认 300，最大 1000**（传 2000 报 `901720 The maximum return number is not valid`）。`data` 是扁平词对象数组 | 2026-11-11 `query=益生菌`：默认→300、maxNum=500→500、1000→1000、2000→901720 |
| 计划列表是否分页 | **分两套，不一样**：搜索 `getCampaign` 无分页参数（`campaignFields/campaignIds/mobileExtend/adType`），空结果 `[]`；电商 `getCampaignShopping` 用 **`limit: [offset, size]`**，且**必传**（不传报 8602） | 2026-11-11 实测：`limit=[0,5]` 与 `[5,5]` 返回不重叠；电商计划共 35 条 |

## 三、字段与写接口

| 问题 | 结论 | 依据 |
|---|---|---|
| 关键词规划师（拓词）全链路 | **已验证**：① `getKRByQuery` 拓词（默认 300/最大 1000 条）② `getKRFileIdByWords` 词表→`fileId` ③ `getFileStatus` 轮询（`isGenerated` 2=处理中、3=已生成）④ `getFilePath` 取 CSV 签名直链（有效期 3600s，未生成完报 `901726`） | 2026-11-11 全流程实跑：fileId `cd9fe925…`，2→3 约 5 秒，下载地址为 `bj.bcebos.com/v1/papfc/krcsv/*.csv` |
| `getKRCustom` 的 `idType` | **已验证**：官方枚举 `3 = 计划 ID`、`5 = 单元 ID`（实测只有这两个合法，`0/1/2/4/6/7/8` 报 `901743`）；`id` 可省略 | 2026-11-11 逐个试探 + 官方文档 |
| 异步词表任务的并发限制 | **单账户同一时间只能有一个任务**：上一个未结束就提交报 `901755 Last file task is not finish` | 2026-11-11 连续提交 100→200 词时实测（先等 `isGenerated=3` 后 200/500/1000 全部成功） |
| `getKRFileIdByWords` 的 `seedWords` 上限 | **1000 个实测通过**（文档字段范围 [1,1000]；服务简介里「单次 100 个」是过时说法） | 2026-11-11 提交 1000 个种子词成功 |
| 拓词返回字段与文档的差异 | 实测多出 `recBid`（推荐出价）、`similar`（相似度）；月均搜索量是**小写 `pv`**（文档写 `PV`）；`wordPackage`/`businessPoints` 恒为空 | 2026-11-11 多次拓词返回样本 |
| `competition` 的取值方向 | **文档自相矛盾**：`getKRByQuery` 页写「1-低 2-中 3-高」，`getKRCustom` 页写「1-高 2-中 3-低」，未交叉验证 | — |
| 计划的暂停字段名 | **已验证（电商推广）：`pause` 布尔值**，`false` 启用 / `true` 暂停；与 `status` 一致（`status=0` 启用 24 条、`status=2` 暂停 11 条） | 2026-11-11 `CampaignShoppingService.getCampaignShopping` 全量 35 条取样 |
| 搜索推广计划的暂停字段名 | **范围外**：本项目只做电商推广线；搜索侧 `getCampaign` 在该账户返回 `[]`，也不打算验 | — |
| 单元 / 关键词 / 创意的暂停字段名 | **无接口可验**：SDK 里没有电商单元/创意的**读**接口（只有 campaign 级），搜索侧又在范围外 | — |
| 写接口必填字段清单 | **已知未知**：缺 `campaignShoppingTypes` 统一报 8602 `_sys`，百度不指出缺哪个字段，只能逐个试 | — |
| 写操作是否幂等 | **已验证幂等**：对已暂停的计划再次 `pause=true`，返回 `succ=1 oprs=1 status=0 failures=[]`，回读 `pause` 仍为 `true`、`status` 仍为 `2` | 2026-11-11 `updateCampaignShopping` 对 2079251858（珠宝专用清洗剂9.7）实测 |
| 写请求的成功判定字段 | **`header.status==0` 且 `failures==[]` 且 `succ==oprs>=1`**；失败时 `status=2`、`succ=0`、`failures[{code,position,message}]`。响应头恒带 `quota`（本次消耗）与 `rquota`（剩余配额，实测 200000000）、`traceid` | 2026-11-11 成功（updateCampaignShopping）与失败（81020）各一次对照 |
| 写操作的生效时延 | **同步生效**：`updateCampaignShopping` 返回后 1 秒回读即是新状态（`pause`/`status` 同时翻转），没有异步等待 | 2026-11-11 恢复→复原各一次，中间态与终态均回读确认 |
| 暂停/恢复是否可回滚 | **可以，且净效果为零**：`pause=false` → `status` 0；`pause=true` → `status` 2；两次往返后与原状态完全一致 | 2026-11-11 计划 2079251858：原 `pause=true/status=2` → 恢复 `false/0` → 复原 `true/2` |
| `budget=0` 的语义 | **不限预算**（该账户 35 条计划全是 `0.0`） | 运营确认，非接口实测；写验证时不改动此字段 |
| 计划配置能否完整读回 | **能，但字段名是扁平的**：要 `audience` 就传 `sex/age/customAge/regionType/region/interests/keywords/keywordsExtend/crowd/etradecrowds/fcKeywordWtpids/intelligentWeight`；要 `material` 就传 `brand/userPortrait/title/description/pictures/videos`。传 `material`/`audience` 这类顶层名会被**静默忽略**（返回空） | 2026-11-11 实测：扁平字段读回 38 关键词 / 367 地域 / 6 标题 / 6 描述 / 18 图 / 1 视频 |
| 计划的名称字段 | 电商计划是 **`campaignShoppingName`**；`campaignName` / `name` 不合法（静默忽略，不报错但也不返回） | 2026-11-11 三个候选字段各试一次 |
| 异步任务如何查终态（能力名 + 判定字段） | **已验证**：`OpenApiReportService.createReportTask` 返回 `taskId` + `taskStatus=RUNNING` → `getTaskStatus(taskId)` 轮询到 `taskStatus=SUCCESS`，终态响应**直接带** `fileUrl`（CSV）、`tableHeader`、`dataStartRow`。两个接口的响应都是单元素数组。实测 ~0.7 秒完成，`wait=True` 轮询 2 次命中 | 2026-11-11 实测 |

## 四、错误码基线

| 错误码 | 含义 | 触发条件 | 来源 |
|---|---|---|---|
| 81020 | `The username information in the request is incorrect.`（`position=_user`） | 请求头 `userName` 不是令牌所属账户的中文名 | 已验证：2026-11-11 错误 `userName` 实测 |
| 9011519 / 1519 | `Request field is invalid`，`position` 指到具体字段 | 字段名不在接口白名单；`AccountService.getAccountInfo` 不传 `accountFields` 也报此码 | 已验证：2026-11-11 四种 `accountFields` 组合实测（仅 `userId` 通过）；报表接口里 `position=_user` 是笼统提示，不代表真跟用户有关 |
| 8602 | `We are unable to serve your request, please try again later.` | `getCampaignShopping` **不传 `limit`**（不是网络问题，重试无用） | 已验证：2026-11-11 缺省 `limit` 时必现，加上即正常 |
| 901720 | `The maximum return number is not valid` | `getKRByQuery` 的 `seedFilter.maxNum` 超过 1000 | 已验证：2026-11-11 传 2000 实测 |
| 901726 | `The request KR File is still in processing` | 词表文件未生成完就调 `getFilePath` | 已验证：2026-11-11 实测（`isGenerated=2` 时） |
| 901743 | `idType is not valid to get KR results` | `getKRCustom` 的 `idType` 不是 3（计划）/ 5（单元） | 已验证：2026-11-11 0-8 逐个试探 |
| 901755 | `Last file task is not finish` | 上一个异步词表任务未完成就提交新任务 | 已验证：2026-11-11 实测 |
| 901400 | `Reporttype could not be empty` / `Unknow reportType: N` | `getReportData` 不传 `reportType`，或传了不存在的值。**实测 1–12 全部 `Unknow`，真实枚举未知** | 已验证：2026-11-11 reportType 1–12 扫描 |
| 30002 | `应用不存在`（OAuth 层，AppID 无效） | 用不存在的 `appId=app-demo` 调 `oauth/accessToken` | 已验证：2026-11-10 本地回调路由实测，返回 `code=30002 message=应用不存在`（说明授权回调 → 换令牌这条链路本身是通的，只差真实 AppID） |

## 五、已知未知（明确记录，不是遗漏）

下面这些**明确没验**，理由也写清楚。它们不进能力说明，也不对外承诺。

| 项 | 为什么没验 | 影响 |
|---|---|---|
| `refreshToken` 轮换语义 | 只在 Casdoor（接入令牌）侧验过刷新可用，百度侧的 refresh 还没真刷过一次 | 决定刷新失败能否重试旧值 |
| 同一 AppID 下多账户 | 只有一个账户 | MCC / 多账户绑定的推断 |
| 写接口必填字段白名单 | 百度只回笼统 8602，不指出字段 | 新建类写接口只能逐个试 |
| 报表单次返回行数上限 | `rowCount=10000` 也只返回 `totalRowCount` 那么多行，没触顶 | 大账户取全量的分页策略 |
| 异步任务的 `FAIL` 形态 | 现有任务都是秒级成功 | 失败时 `fileUrl` 是否为空、错误怎么表达 |
| 智投项目维度 5 个报表（600028/600030/600032/600034/600038） | 账户没有智投项目 | 只能按文档推断 |
| Casdoor 是否真校验 `client_secret` | 需要一次真实授权码 + 错误 secret 才能区分 | 决定「公共客户端」结论是否彻底 |

## 六、维护约定

> **范围声明**：本项目只验证并承诺**电商推广线**（购物推广）。搜索推广、信息流推广的
> 接口仍在能力表里可检索、可调用，但**未经实测、不对外承诺**。

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

5. **新增"SDK 未覆盖"的接口：固定四步，不许靠猜参数。**
   `9015 参数异常` 不带 `position`，凭直觉猜字段名基本命中不了——这三轮踩了三次：
   `keywordPackages`（真名 `keywordPackageTypes`）、`packageIds`（真名
   `delKeywordPackageIds`）、`{field, operatorValue, value}`（真名 `{field, op, values}`）。

   | 步骤 | 动作 | 判据 |
   |---|---|---|
   | ① 判存在性 | 给该 `Service.method` 发**空 body** | `87091 The request url is invalid.` = 路径不存在；`9015 参数异常` = 路径对、接口存在 |
   | ② 要后台抓包 | 控制台 F12 → Network → 看 `params=` / JSON body | 字段名、枚举、数组形状一次到位，比试一百次快 |
   | ③ 实测落证 | 读 → 写 → 回读确认，把证据写进 `verified.basis` | 前端返回 `succ/oprs`、回读状态一致 |
   | ④ 写进文档 | 把真实请求体贴进对应文档（如 `docs/keyword-packages.md`）；参数名与直觉不符时在 `notes` 明写"是 X 不是 Y" | 下一个人不再猜错 |

   ⚠️ **探测写接口时只用空 body**：空 body 只会被判参数异常，不会真建/真删；
   发"看起来合法"的 body 可能真的改到线上数据。
