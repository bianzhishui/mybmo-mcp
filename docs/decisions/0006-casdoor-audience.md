# ADR 0006：Casdoor 的 `aud` 不是资源地址

- 状态：已采纳（2026-11-08，实测后修订）
- 取代：[ADR 0001](0001-auth-architecture.md) 与 `docs/architecture.md`
  第二层中「`aud` 等于本服务规范地址」的假设
- 相关：[ADR 0005](0005-sdk-poc.md)（同样是"先验证再写文档"）

## 实测事实

用 Casdoor 真实签发的 JWT 解码，claims 是：

```json
{
  "iss": "https://auth.pandabase.top",
  "aud": ["60fda1d9bedd4615eb90"],          ← client_id，不是服务地址
  "sub": "ccc3493b-abc5-4f8d-a73a-a2d67e711872",
  "azp": "60fda1d9bedd4615eb90",
  "scope": "openid profile baidu:read baidu:write",
  "exp": 1794715287
}
```

用两种 audience 验证同一个令牌，结果截然不同：

| 校验器的 audience 配置 | 结果 |
|---|---|
| `https://baiduads.pandabase.top`（架构文档的假设） | **被拒**：audience 不是本服务 |
| `60fda1d9bedd4615eb90`（实际的 client_id） | 通过，`sub` 与 scopes 都正确 |

签名与 `iss` 两处都通过了，说明 JWKS 路径（`/.well-known/jwks`）是对的。
**唯一对不上的是 `aud`。**

## 这意味着什么

`docs/architecture.md` 第二层写的这套是 RFC 9068 / RFC 8707 的理想形态：

```text
aud = 本服务规范地址，令牌只能用于这一个资源服务器
```

但 Casdoor 把 `aud` 填成了"这个令牌是发给哪个 OAuth 客户端的"，这是 OIDC 的惯例
（`aud` 标识客户端），不是资源服务器的惯例。两者语义不同，不能靠配置调和。

后果分三种情况：

1. **严格按 `aud == 资源地址` 校验** → Casdoor 签的所有令牌都被拒，服务不可用。
2. **改成 `aud == client_id` 校验** → 能用，但丢掉一层保护：
   任何拿到**同一个 client** 令牌的人都能调本服务。而 DCR 允许自助注册，
   注册出来的每个 client 都有自己的 client_id，所以**别的 client 的令牌仍会被拒**。
3. **完全不校验 `aud`** → 同一 issuer 下任何应用的令牌都能进，包括用户自己
   在 Casdoor 里为别的用途建的应用。不能接受。

## 决定

采用方案 2，但把语义写清楚：**`aud` 校验的对象是"允许调用本服务的 OAuth 客户端"**，
不是"资源地址"。

```python
JwtVerifier(
    issuer="https://auth.pandabase.top",
    audience="60fda1d9bedd4615eb90",  # mybmo-mcp 这个应用的 client_id
)
```

这样保住的那个保护是真实的：**只有为本服务注册的 client 签发的令牌才能进来**。
丢掉的那个（RFC 8707 resource indicator）在 Casdoor 上本来就不存在，不是我们放弃的。

## 代价与遗留

- `MYBMO_RESOURCE_URL` 从"audience 校验值"退回"元数据里的 resource 字段 + 日志用"。
  它仍然必要（RFC 9728 的 `/.well-known/oauth-protected-resource` 要它），但不再参与验签。
- 新增 `MYBMO_OIDC_AUDIENCE`：显式配 client_id。**不设就拒绝启动**——
  猜一个默认值比报错更危险。
- `AuthSettings.validate_token_resource` 必须设 `False`：
  它的语义是"令牌的 resource 字段要等于 `resource_server_url`"，而 Casdoor 不签发
  resource 字段。设为 `True` 会拒掉所有令牌。
- 换 Casdoor 版本或改用别的授权服务器时，**必须重新实测 `aud`**。
  这条不是 Casdoor 独有的怪癖，而是"授权服务器把 aud 填成 client_id"这类实现的共性。

## 为什么现在才发现

P4 的代码在写的时候是照 `docs/architecture.md` 实现并单测的——测试里自己造令牌，
`aud` 自然填成了资源地址，所以 14 个用例全绿。**单测验证的是"代码符合我写的规格"，
不是"规格符合真实授权服务器"**。

这和 ADR 0005 是同一类教训：SDK 的字段结构也是先写错、读真实代码才发现。
区别是那次靠读源码，这次只能靠真跑。

结论：涉及外部系统的接口，**规格必须由对方的实际输出确定，不能由我们的文档确定**。
