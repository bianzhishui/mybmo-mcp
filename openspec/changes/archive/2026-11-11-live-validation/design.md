# 设计：真实链路验证

## 验证矩阵

按「风险最高、影响面最广」挑代表，不追求覆盖全部 390 条。

| 能力 | 验什么 | 失败说明什么 |
|---|---|---|
| 账户查询（`AccountService`） | `userName` 与令牌账户是否一致、81020 的触发条件 | 账户绑定模型错了 |
| 计划列表（`CampaignService`） | 字段命名、`campaignTypes[]` 层级、空列表的形状 | schema 生成或字段驼峰对齐有偏差 |
| 关键词查询（`KRService.getKRByQuery`） | 单次返回上限（资料称 300） | descriptions 的 `notes` 是错的 |
| 报表（`OpenApiReportService`） | 行数上限、分页字段、时间范围约束 | `report.json` fixture 与实际不符 |
| 计划暂停 → 恢复 | 写接口必填字段、错误码分布、幂等性 | 写路径不可用 |

选择「暂停 → 恢复」作为写验证，是因为它**可回滚**：即使中途失败也能恢复原状态。

## 六项未知项的验证方法

| 未知项 | 方法 | 落点 |
|---|---|---|
| `state` 是否原样回传 | 绑定流程里带自定义 state，观察回调参数 | `docs/baidu-behavior.md` + `auth/binding.py` 注释 |
| `refreshToken` 是否轮换 | 连续两次刷新，比较新旧 refreshToken；再用旧值试一次 | `docs/baidu-behavior.md` + `auth/service.py` |
| 报表单次上限与分页 | 拉一个超出预期行数的时间范围，看返回条数与是否有下一页标记 | `descriptions.json` 的 `notes` |
| 暂停字段名（计划/单元/关键词/创意） | 四个层级各调一次查询，读返回里的状态字段 | `descriptions.json` 的 `params` |
| `getKRByQuery` 上限 | 用宽泛词根拉一次，数返回条数 | `descriptions.json` 的 `notes` |
| 写接口必填字段 | 故意缺字段，记录真实错误码与提示 | `docs/baidu-behavior.md` |

结论统一写在 `docs/baidu-behavior.md`：**只记结论与依据，不记尝试过程**。能力级的细节
（单位、层级、易错点）写进 `descriptions.json`，让 agent 在 schema 里直接看到。

## `wait=True` 的实现

轮询协议定义在 ADR 0004 D5，但**能力表里目前没有 `async_task`**——哪些能力是异步、
任务字段叫什么，要等实测确认后补进 `capabilities.json`（见 9.x）。因此执行器按
「元数据存在就轮询、缺失就忽略并说明」实现，机制本身可以离线测。

```text
execute(cap, input, wait=False)      → 原样返回提交结果
execute(cap, input, wait=True)       → 取 task_id（按元数据的 task_id_field）
                                     → 轮询 status_capability 到 done_when 命中
                                     → 若有 result_capability，再取一次结果
                                     → 超时：返回 {ok: true, data: {timed_out: true, ...}}
```

三条约束：

- **超时不报错**：异步任务的语义是"已提交"，超时只说明还没跑完，不是失败
- **轮询间隔递增**（1s → 2s → 4s，上限 10s），避免对百度接口造成浪费
- **总时长由元数据的 `timeout` 决定**（ADR 示例 60s；报表类可能要调到 300s）

## 风险

| 风险 | 缓解 |
|---|---|
| 写操作影响真实账户 | 只做「暂停 → 恢复」，且先记录原状态 |
| 报表拉取过大 | 先用小时间范围，确认上限后再放大 |
| 真实响应与 fixture 差异大导致测试失败 | fixture 更新与代码修复分开提交，避免混在一起判断 |

## 决定

1. 代表能力按上表选，**其余能力靠同类推断**，不逐个验证
2. 实测结论进 `docs/baidu-behavior.md`，不写"我们试了三次"这类过程叙事
3. `wait=True` 先实现再验证：没有真实异步任务时用 fixture 覆盖分支
