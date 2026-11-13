"""运行配置。

所有敏感项只从环境变量或 ``.env`` 读取，不写进代码、不写进仓库。
``.env`` 已被 ``.gitignore`` 排除；生产环境用系统环境变量或密钥管理服务注入。
"""

from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mybmo_mcp.baidu.errors import CredentialError

MASTER_KEY_BYTES = 32


class Settings(BaseSettings):
    """环境配置。字段名与 ``.env.example`` 中的变量一一对应（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    baidu_app_id: str = ""
    baidu_secret_key: str = ""
    mybmo_master_key: str = ""
    casdoor_issuer: str = ""
    mybmo_resource_url: str = ""
    #: 百度 OAuth 的授权回调地址，必须与在 dev2 应用「安全设置」里登记的**根域**一致，
    #: 且不能带 ``?`` 或 ``%``（百度侧不允许把参数塞进 callback，附加信息走 state）。
    #: 只在 ``auth bind`` 与回调端点里用到；为空时这两个入口直接报错，不影响其他功能。
    mybmo_baidu_callback_url: str = ""
    #: 百度订阅（事件推送）的共享密钥。**不配则 ``/oauth/baidu/subscribe`` 直接 503**，
    #: 失败关闭——宁可端点不可用，也不能悄悄变成谁都能灌数据的匿名接收器。
    #: 它与 ``BAIDU_SECRET_KEY``（应用密钥）和 ``MYBMO_MASTER_KEY``（凭据加密主密钥）
    #: 是三个互不相干的东西，别混用。
    mybmo_baidu_subscribe_secret: str = ""
    #: JWT 的 audience **白名单**。**不是资源地址，是 OAuth 客户端的 client_id**
    #: （IdP 把 aud 填成 client_id，与 RFC 8707 的资源标识语义不同，见 docs/auth-contract.md）。
    #: 多个客户端用逗号分隔；每个都必须先在 IdP 里显式注册（不开 DCR）。
    #: 不设就拒绝启动——猜一个默认值比报错更危险。
    mybmo_oidc_audience: str = ""
    #: 允许的 **Casdoor 组织**白名单（逗号分隔）。本服务只对应 pandabase 组织下的
    #: mybmo-mcp 应用，所以这里通常只有一个值。留空 = 不校验（仅靠 aud 白名单）。
    #: 校验发生在鉴权层：令牌的 ``owner`` 声明不在列表里直接 401，
    #: 授权表里**不再**重复表达组织维度。
    mybmo_oidc_orgs: str = ""
    mybmo_jwks_url: str = ""
    mybmo_database_url: str = ""
    #: 旧配置项，仅 ``local`` 模式与 ``auth bind`` 兜底还在用；本地模式下线后一并删除
    mybmo_tenant_id: str = "t_default"
    #: 是否把入参/出参正文也写进审计。关掉只留 payload_digest
    mybmo_audit_store_payload: bool = True
    #: 是否额外挂一个**只读** MCP 入口（7 个工具，写能力在入口处被拒）。
    #: 默认关闭时进程装配与行为完全不变，见 ADR 0011。
    #: 典型用法：多 Agent 链路里"非实施角色"只能挂只读入口，写边界由结构保证。
    mybmo_mcp_readonly: bool = False
    #: 只读入口路径。必须与全量入口 ``/mcp`` 不同，否则两条入口会互相遮住。
    mybmo_mcp_readonly_path: str = "/mcp/readonly"

    @field_validator("mybmo_mcp_readonly", mode="before")
    @classmethod
    def _empty_switch_means_off(cls, value: Any) -> Any:
        """``MYBMO_MCP_READONLY=`` （置空）等于关闭，不是配置错误。

        置空是最自然的"把这个开关关掉"写法，也是回滚文档里那一行。pydantic 默认会把
        空串判成非法布尔值，让**整个服务起不来**——回滚动作反而把服务打死，这是最坏的
        失败模式。同理接受 ``on/off/yes/no``，但只对这一个开关放宽（其余开关维持严格）。
        """
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ("", "0", "false", "no", "off"):
                return False
            if text in ("1", "true", "yes", "on"):
                return True
        return value

    def master_key_bytes(self) -> bytes:
        """解码并校验主密钥。

        密钥格式错了必须立刻失败——不能让服务带着一个坏密钥启动，
        否则第一次写凭据时才会炸，而那时可能已经覆盖了旧文件。
        """
        raw = self.mybmo_master_key.strip()
        if not raw:
            raise CredentialError(
                "缺少 MYBMO_MASTER_KEY。生成方式："
                'python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"'
            )
        try:
            key = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CredentialError("MYBMO_MASTER_KEY 不是合法的 base64 字符串") from exc
        if len(key) != MASTER_KEY_BYTES:
            raise CredentialError(
                f"MYBMO_MASTER_KEY 解码后必须是 {MASTER_KEY_BYTES} 字节，当前 {len(key)} 字节"
            )
        return key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试中如需替换，用 ``get_settings.cache_clear()``。"""
    return Settings()
