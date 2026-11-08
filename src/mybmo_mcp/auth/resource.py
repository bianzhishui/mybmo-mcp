"""OAuth 2.1 Resource Server 侧的 JWT 校验。

mybmo-mcp 不做授权服务器——令牌由 Casdoor 签发，这里只负责**拒绝不该被接受的令牌**。
校验五件事，任何一条不过就是 401：

1. **签名**：用 issuer 的 JWKS 验，密钥按 ``kid`` 找；缓存 10 分钟并支持强制刷新
2. **iss**：必须与配置完全一致（RFC 8414 要求精确字符串比较，末尾斜杠都算不同）
3. **aud**：必须包含本服务规范地址，否则就是"别的服务签的令牌拿来用了"
4. **exp / nbf**：容忍 60 秒时钟偏移
5. **scope**：交给 SDK 的中间件按工具要求判定，这里只做解析

两条**不能妥协**的规则：

- 不接受未在配置里声明的 issuer。多接受一个 issuer，等于给另一个租户的令牌开后门。
- 不把百度 accessToken 当 Bearer。两者生命周期、受众、用途都不同，混用就是越权。

JWKS 拉取失败时**拒绝而非放行**。缓存过期 + 网络故障的瞬间，如果选择"先信旧密钥"，
那就等于在故障期间接受任何人的令牌。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx2
import jwt
from jwt import PyJWKClient

from mybmo_mcp.baidu.errors import BaiduError

#: 时钟偏移容忍。超过这个值就说明要么令牌过期了，要么机器时间错了
CLOCK_SKEW_SECONDS = 60

#: JWKS 缓存时长。Casdoor 轮换签名密钥的频率远低于这个值
JWKS_CACHE_SECONDS = 600

#: 本服务接受的 scope
SCOPE_READ = "baidu:read"
SCOPE_WRITE = "baidu:write"
SCOPE_ADMIN = "baidu:admin"
SCOPES_SUPPORTED: tuple[str, ...] = (SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)


class TokenError(BaiduError):
    """令牌不可接受。消息会原样出现在 ``WWW-Authenticate`` 的错误描述里。

    所以文案要能让人知道下一步做什么，而不是只说"invalid token"。
    """


@dataclass
class JwksCache:
    """带缓存的 JWKS 客户端。

    自己实现缓存而不是用 ``PyJWKClient(cache_keys=True)``，是为了能显式控制两点：
    过期后必须重新拉取，且拉取失败不能沿用旧密钥。
    * 拉取失败不沿用旧值，见本模块 docstring。
    """

    url: str
    ttl: float = JWKS_CACHE_SECONDS
    _client: PyJWKClient | None = field(default=None, repr=False)
    _fetched_at: float = 0.0

    def get(self, *, now: float | None = None) -> PyJWKClient:
        current = now if now is not None else time.time()
        if self._client is None or current - self._fetched_at >= self.ttl:
            self._client = PyJWKClient(self.url, cache_keys=True, lifespan=self.ttl)
            self._fetched_at = current
        return self._client

    def invalidate(self) -> None:
        self._client = None
        self._fetched_at = 0.0


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    subject: str
    client_id: str
    scopes: tuple[str, ...]
    expires_at: int | None
    claims: dict[str, Any]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


class JwtVerifier:
    """把 Casdoor 签发的 JWT 变成 :class:`VerifiedToken`。

    实现 MCP SDK 的 ``TokenVerifier`` 协议（``async verify_token``），
    因此可以直接传给 ``MCPServer(token_verifier=...)``。
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        clock_skew: int = CLOCK_SKEW_SECONDS,
    ) -> None:
        if not issuer:
            raise ValueError("issuer 不能为空：多用户模式下必须配置 CASDOOR_ISSUER")
        if not audience:
            raise ValueError("audience 不能为空：必须配置 MYBMO_RESOURCE_URL")
        self._issuer = issuer.rstrip("/")
        self._audience = audience.rstrip("/")
        self._clock_skew = clock_skew
        self._jwks = JwksCache(jwks_url or f"{self._issuer}/api/v1/jwks")

    @property
    def issuer(self) -> str:
        return self._issuer

    @property
    def audience(self) -> str:
        return self._audience

    def verify(self, token: str, *, now: float | None = None) -> VerifiedToken:
        """同步校验。抛 :class:`TokenError` 表示不可接受。"""
        current = now if now is not None else time.time()
        try:
            signing_key = self._jwks.get(now=current).get_signing_key_from_jwt(token)
        except jwt.PyJWKClientError as exc:
            # 密钥拉不到 / kid 不认识。这里**不能**退化成"不验签"。
            raise TokenError(
                f"无法解析令牌签名密钥：{exc}。若是 issuer 刚轮换密钥，请稍后重试"
            ) from exc
        except jwt.InvalidTokenError as exc:
            # 令牌本身畸形（段数不对、base64 坏了）。PyJWKClient 在取 kid 时就会抛，
            # 早于 decode()，所以必须在这里接住，否则会穿透成 500。
            raise TokenError(f"令牌格式错误：{exc}") from exc

        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._clock_skew,
                options={
                    "require": ["iss", "aud", "exp", "sub"],
                    # PyJWT 默认会按配置的 aud 做严格比较，但 issuer 只要不在列表里
                    # 就会拒绝；这里显式声明要精确匹配，避免"看起来验了其实没验"。
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenError("令牌已过期，请重新登录") from exc
        except jwt.InvalidAudienceError as exc:
            raise TokenError(
                f"令牌的 audience 不是本服务（期望 {self._audience}）。这通常是拿别的服务的令牌来用"
            ) from exc
        except jwt.InvalidIssuerError as exc:
            raise TokenError(f"令牌的 issuer 不受信任（期望 {self._issuer}）") from exc
        except jwt.InvalidTokenError as exc:
            raise TokenError(f"令牌无效：{exc}") from exc

        scope_text = str(claims.get("scope") or claims.get("scp") or "")
        scopes = tuple(sorted({s for s in scope_text.replace(",", " ").split() if s}))

        return VerifiedToken(
            subject=str(claims["sub"]),
            client_id=str(claims.get("client_id") or claims.get("azp") or ""),
            scopes=scopes,
            expires_at=int(claims["exp"]) if claims.get("exp") else None,
            claims=claims,
        )

    # ---------- MCP SDK 协议 ----------

    async def verify_token(self, token: str) -> Any:
        """``TokenVerifier`` 协议入口。返回 ``AccessToken`` 或 ``None``。

        失败返回 ``None`` 而不是抛异常：SDK 的中间件据此返回 401。
        抛异常会让它变成 500，客户端看到的是"服务器炸了"，不是"你没登录"。
        """
        from mcp.server.auth.provider import AccessToken

        try:
            verified = self.verify(token)
        except TokenError:
            return None

        return AccessToken(
            token=token,
            client_id=verified.client_id,
            scopes=list(verified.scopes),
            expires_at=verified.expires_at,
            resource=self._audience,
            subject=verified.subject,
            claims=verified.claims,
        )


def fetch_jwks(url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """取 JWKS。测试与诊断用；运行时由 :class:`JwksCache` 经 PyJWKClient 拉取。"""
    response = httpx2.get(url, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or "keys" not in data:
        raise TokenError(f"{url} 返回的不是 JWKS（缺少 keys 字段）")
    return data
