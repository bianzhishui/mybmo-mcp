"""百度营销开放平台访问层。

- ``catalog``：接口目录（Service / Method / 读写 / 产品线），随包分发
- ``errors``：错误码释义、凭据脱敏、failed / unknown 语义
- ``transport``：原生 HTTP 调用（SDK 不可用时的 fallback；当前不参与主路径）

**这里不实现任何业务语义。** 拓词、快照聚合、报表编排属于调用方的工作流，
能力层只负责"把某个 Service.method 忠实执行掉"。
"""
