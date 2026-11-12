# 变更：真实链路验证（live-validation）

> **状态**：✅ 已完成（32/32 任务，2026-11-11）。结论沉淀在 `docs/baidu-behavior.md`、
> `docs/reports.md`、`docs/keyword-planner.md`、`docs/keyword-packages.md`；
> 实测能力以 `descriptions.json` 的 `verified` 标记为准（线上 25 条）。

## 为什么

390 个能力目前只有元数据，**没有任何一次真实调用**。由此带来三个具体问题：

1. 六项百度行为仍是推断（`state` 是否回传、`refreshToken` 是否轮换、报表与拓词的单次上限、
   暂停字段名、写接口必填字段与错误码分布）。推断错了，能力表就是"看起来完整"的假象。
2. 所有能力的成熟度都停在 `experimental`，无法对外承诺任何一条能跑通。
3. `execute_capability(wait=True)` 还没实现——轮询逻辑的正确性只能靠真实异步任务验证。

## 变更内容

1. **绑定一个真实百度账户**，走完 `auth bind` → 回调 → 落库 → `list_accounts` 能看到。
2. **按验证矩阵跑代表性能力**（**电商线**：账户、计划、报表、一次可回滚的写操作），
   把实测结果写成当前事实（`docs/baidu-behavior.md`），并回填 `descriptions.json` 的
   `notes` / `params` 与 `tests/fixtures/`。
3. **实现 `wait=True` 轮询**：按能力的 `async_task` 元数据轮询，超时返回
   `{timed_out: true}` 而不是报错。
4. **真实客户端接入**：用 Claude Desktop 或 Cursor 走一次授权码 → 登录 → 调一个能力，
   确认"手工注册 + 回调登记"这条路对真实客户端成立。
5. 把已验证的能力从 `experimental` 提升，并记录每个能力"验过什么"。

## 不做

- **不做电商以外的推广线**：本项目只服务电商推广（购物推广）。搜索推广、信息流推广的
  验证券（`getCampaign` 层级、关键词/创意暂停字段、搜索/信息流的 `reportType`）**整体移出范围**，
  能力表里那些接口保持"仅按文档推断"的状态，不对外承诺
- 不追求覆盖全部 390 个能力：按风险选代表，其余靠同类推断
- 不做写操作的批量验证：只挑一个可回滚的写动作
- 不改架构：仍是 `local` / `oidc` 双模式，不碰基础设施

## 影响面

| 区域 | 变化 |
|---|---|
| `docs/baidu-behavior.md` | 新增：实测结论表（当前事实，不是过程记录） |
| `baidu/descriptions.json` | 50 条精写说明按实测修订；补分页/上限等 `notes` |
| `tests/fixtures/` | 用真实响应替换占位，特别是 `report.json` 的分页字段 |
| `tools/capability.py` | 新增 `wait=True` 轮询分支 |
| `docs/auth-contract.md` | 只在百度侧结论影响鉴权时更新（预计不动） |

## 验收标准

- [ ] 一个真实账户完成绑定，`list_accounts` 返回它的中文名与可用状态
- [ ] 五类能力各至少一次成功调用，响应结构与 fixture 一致
- [ ] 六项未知行为全部有结论，写进 `docs/baidu-behavior.md`，不留"待确认"
- [ ] `wait=True` 对一个真实异步任务返回终态；超时返回 `{timed_out: true}`
- [ ] 一个真实 MCP 客户端（Claude Desktop / Cursor）完成一次工具调用
- [ ] 至少一条能力从 `experimental` 提升，并注明验证依据
