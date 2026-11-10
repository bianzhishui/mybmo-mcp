"""运行配置。

所有敏感项只从环境变量或 ``.env`` 读取，不写进代码、不写进仓库。
``.env`` 已被 ``.gitignore`` 排除；生产环境用系统环境变量或密钥管理服务注入。
"""

from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from pathlib import Path

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
    mybmo_data_dir: Path = Path("./data")
    mybmo_master_key: str = ""
    #: local = stdio 单用户，本机进程即身份；oidc = 多用户，身份来自 JWT。
    #: 取任何其他值都按多用户处理（失败关闭），避免拼错导致越权。
    mybmo_auth_mode: str = "local"
    casdoor_issuer: str = ""
    mybmo_resource_url: str = ""
    #: 百度 OAuth 的授权回调地址，必须与在 dev2 应用「安全设置」里登记的**根域**一致，
    #: 且不能带 ``?`` 或 ``%``（百度侧不允许把参数塞进 callback，附加信息走 state）。
    #: 只在 ``auth bind`` 与回调端点里用到；为空时这两个入口直接报错，不影响其他功能。
    mybmo_baidu_callback_url: str = ""
    #: JWT 的 audience **白名单**。**不是资源地址，是 OAuth 客户端的 client_id**
    #: （IdP 把 aud 填成 client_id，与 RFC 8707 的资源标识语义不同，见 docs/auth-contract.md）。
    #: 多个客户端用逗号分隔；每个都必须先在 IdP 里显式注册（不开 DCR）。
    #: 不设就拒绝启动——猜一个默认值比报错更危险。
    mybmo_oidc_audience: str = ""
    mybmo_jwks_url: str = ""
    mybmo_database_url: str = ""

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
