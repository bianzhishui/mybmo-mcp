# ADR 0005：SDK 可行性验证结论——只做元数据源，不做执行器

- 状态：已接受
- 日期：2026-11-07
- 关联：[ADR 0003](0003-capability-only.md)（能力层）、[ADR 0004](0004-multi-tenant-accounts.md)（多租户）

## 验证环境

Python 3.12 + uv，`baiduads-sdk==2023.1.0`（依赖 `baiduads-sdk-auto==2023.1.0`）。
解析出的传递依赖：`urllib3 2.8.0`、`python-dateutil 4.0.0`、`tzdata`、`six` 无冲突。
**此前"SDK 传递依赖带老版 urllib3"的担忧不成立**——`urllib3>=1.25.3` 没有上界。

## 结论

| 能力 | 可用 | 证据 |
|---|---|---|
| 在 Python 3.12 安装 | ✅ | 无版本冲突 |
| 端点路径枚举 | ✅ | `/json/sms/service/AccountService/getAccountInfo`、`/json/feed/v1/CampaignFeedService/getCampaignFeed`，与自建 transport 完全一致 |
| 请求序列化 snake→camel | ✅ | `account_fields`→`accountFields`、`keyword_types`→`keywordTypes` |
| 字段表 | ✅ 但**必须递归** | 见下方"嵌套结构" |
| **作为执行器** | ❌ | 两个硬缺陷，见下 |

### 嵌套结构（曾误判为"字段缺失"）

`ApiCampaignAddRequest` 只有 2 个字段：`campaignTypes` 与 `adType`。
真正的 `campaignName` / `budget` / `pause` 在下一层 `ApiCampaignType`（39 个字段）里。
同理 `ApiAdgroupAddRequest`（`adgroupTypes`）、`ApiCreativeAddRequest`（`creativeTypes`）、
`ApiKeywordAddRequest`（`keywordTypes`，`adgroupId` 在 `ApiKeywordType` 的 31 个字段中）。

**生成 schema 时必须递归下钻**，否则会得出"这个接口没有参数"的错误结论。

## 两个硬缺陷

### 1. 不校验未知字段

```python
ApiKeywordAddRequest(nonsense_field="x")
# 序列化结果：{"nonsense_field": "x"}
```

模型本质是"带类型提示的 dict"，不是校验器。未知字段被**原样静默序列化并发出去**。
用它构造请求等于放弃参数校验：模型把 `adgroupId` 写成 `adgroup_id`，不会被拦，
会带着错误字段名打到百度，换回一个与真实原因无关的错误码。

### 2. 响应反序列化严格，且不可关闭

`ApiResponseHeader.rquota` 声明为 `int`。返回里若出现对象或其他形状，
SDK 在 `validate_and_convert_types` 抛 `ApiTypeError`，**整次调用失败**。
生成的服务方法把 `response_type` 写死，外部无法关闭校验。

2023 年生成的模型不应成为运行时正确性的前置条件——线上字段形状只要有一处变化，
所有调用一起挂。

## 决策

**SDK 只用作元数据源，不进入运行时。**

| 阶段 | 做法 |
|---|---|
| 构建期 | 内省 SDK → 生成 `capabilities.json`：端点路径、字段树（递归）、类型、`mutating` 初判 |
| 运行期 | 读 `capabilities.json` + 自建 `transport.py`（httpx2 异步）发送 |
| 参数校验 | 用生成的字段表自己做：未知名拒绝、snake/camel 互转、类型检查 |
| 依赖位置 | `baiduads-sdk` 从 runtime 移到 `[dependency-groups] dev` |

## 理由

1. **不阻塞事件循环**：SDK 基于同步 `urllib3`，而 MCP 工具是 async，每次调用都要 `to_thread`；用自建 transport 直接就没有这个问题
2. **不让响应漂移拖垮运行时**：线路上的 JSON 由我们自己解析，字段多了少了都能容错
3. **保留 SDK 真正的价值**：399 个端点、字段名、嵌套关系、类型 —— 这些是它擅长的
4. **运行时依赖更小**：生产镜像不需要 `baiduads-sdk` 那 6 MB

## 代价

| 代价 | 说明 |
|---|---|
| 要写生成器 | 内省 + 递归下钻 + 输出 JSON，约 1 天 |
| 需随 SDK 更新重跑 | `capabilities.json` 纳入版本控制，diff 可见 |
| 覆盖受 SDK 版本限制 | 2023 之后新增的接口仍需裸调（`execute_capability` 允许未收录 key） |

## 未决问题

- [ ] `rquota` 的真实形状（int 还是对象）：SDK 声明 int，旧实现的 fixture 写成对象，需真实账户确认
- [ ] 嵌套结构如何呈现给 agent：展开成 `campaignTypes[].campaignName` 点路径，还是保留嵌套层级
- [ ] SDK 里 `*Add*` 之外的部分接口是否也有嵌套缺失，需要抽样全部 129 个服务后再下结论
