# 测试用响应样例

这些文件是按百度营销开放平台的**公开响应结构手工构造**的，不是真实账户抓包。
字段名与层级参考了 `api-index.md` 与既有实现的调用记录。

用途是固定住解析逻辑的输入形状，让传输层测试不依赖网络。

| 文件 | 接口 | 覆盖点 |
|---|---|---|
| `account_info.json` | `AccountService/getAccountInfo` | 正常响应、`rquota` 读取 |
| `kr_expand.json` | `KRService/getKRByQuery` | 关键词列表、`KR` 大写路径 |
| `report.json` | `OpenapiReportService/getReportData` | 报表行、分页字段占位 |
| `error_81020.json` | 任意 | `status != 0`、`failures[0]` 提取 |

**接入真实账户后必须用实际响应替换并重新核对**，特别是 `report.json` 的分页字段
（单次返回上限与是否支持分页仍是未决项）。
