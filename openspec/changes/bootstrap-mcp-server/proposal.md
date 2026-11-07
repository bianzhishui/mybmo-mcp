## Why

百度营销的投放日常有三类重复劳动：查账户数据、拓词、批量改投放。
这些操作百度后台能做，但跨账户、跨层级时效率很低；而现有的 MCP 生态里没有可用的百度营销服务。

同时，把百度 API 直接暴露给 AI 客户端存在两个真实风险：

- **越权**：模型可以指定任意 `accountId`，服务端若不校验归属就会串户
- **误写**：一句"帮我把这个词加进去"就可能产生真实扣费，没有人工确认环节

因此需要一个服务，既提供 AI 友好的工具，又把「账户归属」和「写操作确认」做成不可绕过的约束。

## What Changes

- 新增 MCP 服务骨架：stdio 与 Streamable HTTP 两种传输，`mybmo-mcp` 命令行入口
- 新增百度传输层：网关请求构造、错误归一化、读写重试策略分离
- 新增账户与凭据模块：多账户注册、别名隔离、令牌刷新轮换、加密存储
- 新增百度 OAuth 绑定流程：一次性 state、回调收码、`getUserInfo` 落库
- 新增 Casdoor 接入：MCP 侧作为 OAuth 2.1 Resource Server，校验 JWT 与 audience
- 新增只读工具：`list_accounts`、`account_snapshot`、`auth_status`、`keyword_plan`、
  `query_report`、`get_entity`、`list_entities`、`api_describe`、`api_read`
- 新增写操作闭环：`change_preview` → `approval_id` → `change_execute`
- 新增多租户与审计：`tenant` / `account_grants` / 四态审计日志
- 新增部署产物：Casdoor + 服务端 compose、Caddy 配置、运维说明

不在本次范围：399 接口的全量映射、竞价策略算法、前端管理界面。

## Impact

- **Affected specs**: `specs/mcp-server`、`specs/baidu-transport`、`specs/account-vault`、
  `specs/oauth-binding`、`specs/mcp-authorization`、`specs/tool-read`、
  `specs/write-approval`、`specs/multi-tenancy`
- **Affected code**: 全新项目，无既有代码需要迁移
- **外部依赖**: 需要可用百度营销应用（AppID/SecretKey）与至少一个可授权账户才能完成 P3 之后的验收
- **可回滚性**: 全部新增，删除仓库即可回滚；不影响任何既有系统
