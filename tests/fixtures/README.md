# 测试用响应样例

这些文件固定住解析逻辑的输入形状，让传输层与工具层测试不依赖网络。
**凡是标了"实测"的都是真实账户抓包**，其余按公开文档结构构造。

| 文件 | 接口 | 覆盖点 | 来源 |
|---|---|---|---|
| `account_info.json` | `AccountService/getAccountInfo` | 正常响应、`rquota` 读取 | 构造 |
| `kr_expand.json` | `KRService/getKRByQuery` | 关键词列表、`KR` 大写路径 | 构造 |
| `report.json` | `OpenApiReportService/getReportData` | 报表行、`rowCount`/`totalRowCount` 分页字段 | **实测 2026-11-11** |
| `report_task.json` | `createReportTask` + `getTaskStatus` | 异步任务：`RUNNING` → `SUCCESS` + `fileUrl`/`tableHeader` | **实测 2026-11-11** |
| `error_81020.json` | 任意 | `status != 0`、`failures[0]` 提取 | 构造 |

三点与旧版不同，都是实测纠正过的：

1. **真实响应有两层**：`{header, body:{data, expand}}`。传输层把 `body.data` 拆出来，
   `rquota` 在 `header` 里且是**整数**（不是 `{quota, used, limit}` 对象）。
2. **报表分页字段是 `rowCount` / `totalRowCount`**，请求侧用 `startRow` / `rowCount`。
   默认排序不稳定，取全量必须显式传 `sorts`。
3. **异步接口的响应是单元素数组**：`taskId` / `taskStatus` / `fileUrl` 都在 `[0]` 里。

`report_task.json` 里的 `fileUrl` 签名已脱敏（原值只有 5 分钟有效期）。
