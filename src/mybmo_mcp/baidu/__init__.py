"""百度营销开放平台访问层。

- ``capabilities``：能力注册表（390 条接口 + 587 个模型 + 中文检索），构建期从 SDK 内省生成
- ``descriptions``：手写能力描述，加载时合并进注册表与 JSON Schema
- ``errors``：错误码释义、凭据脱敏、failed / unknown 语义
- ``transport``：原生 HTTP 调用；SDK 的反序列化不可关闭，所以只做元数据源

**这里不实现任何业务语义。** 拓词、快照聚合、报表编排属于调用方的工作流，
能力层只负责"把某个 Service.method 忠实执行掉"。
"""
