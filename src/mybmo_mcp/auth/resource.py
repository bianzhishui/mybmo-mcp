"""OAuth 2.1 Resource Server 侧的 JWT 校验。

mybmo-mcp 不做授权服务器——令牌由 Casdoor 签发，这里只负责**拒绝不该被接受的令牌**。
校验五件事，任何一条不过就是 401：

1. **签名**：用 issuer 的 JWKS 验，密钥按 ``kid`` 找；缓存 10 分钟并支持强制刷新
2. **iss**：必须与配置完全一致（RFC 8414 要求精确字符串比较，末尾斜杠都算不同）
3. **aud**：必须命中白名单里的某个 client_id，否则就是"别的客户端签的令牌拿来用了"
4. **exp / nbf**：容忍 60 秒时钟偏移
5. **scope**：交给 SDK 的中间件按工具要求判定，这里只做解析

两条**不能妥协**的规则：

- 不接受未在配置里声明的 issuer。多接受一个 issuer，等于给另一个租户的令牌开后门。
- 不把百度 accessToken 当 Bearer。两者生命周期、受众、用途都不同，混用就是越权。

JWKS 拉取失败时**拒绝而非放行**。缓存过期 + 网络故障的瞬间，如果选择"先信旧密钥"，
那就等于在故障期间接受任何人的令牌。

关于 ``aud``：Casdoor 把 ``aud`` 填成**客户端的 client_id**，不是 RFC 8707 的资源标识
（实测记录见 docs/auth-contract.md）。所以这里的 audience 是一个**白名单**——每个在
Casdoor 里显式注册、允许调用本服务的客户端，把它的 client_id 加进来。DCR 动态注册的
客户端拿到的 aud 是新 client_id，不在白名单里，因此**无法调用本服务**。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

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
    #: 令牌声明的 aud 中，命中白名单的那一个（Casdoor 里就是 client_id）
    audience: str
    scopes: tuple[str, ...]
    expires_at: int | None
    claims: dict[str, Any]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def _normalize_audiences(value: str | Sequence[str]) -> tuple[str, ...]:
    """把配置值变成去重后的白名单。

    支持三种写法：单个 client_id、逗号分隔的一串、以及已经切好的序列。
    允许尾斜杠差异——``AnyHttpUrl`` 会强制补斜杠，配置里手写时未必带。
    """
    raw = value.split(",") if isinstance(value, str) else list(value)
    seen: list[str] = []
    for item in raw:
        text = str(item).strip().rstrip("/")
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)


def _matched_audience(claim: Any, allowed: tuple[str, ...]) -> str:
    """从令牌的 ``aud`` 里挑出命中白名单的那个。

    走到这里说明 PyJWT 已经确认至少命中一个，所以兜底返回 ``allowed[0]`` 只是防御性写法。
    """
    values = [claim] if isinstance(claim, str) else list(claim or [])
    for item in values:
        text = str(item).rstrip("/")
        if text in allowed:
            return text
    return allowed[0]


class JwtVerifier:
    """把 Casdoor 签发的 JWT 变成 :class:`VerifiedToken`。

    实现 MCP SDK 的 ``TokenVerifier`` 协议（``async verify_token``），
    因此可以直接传给 ``MCPServer(token_verifier=...)``。
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str | Sequence[str],
        jwks_url: str | None = None,
        clock_skew: int = CLOCK_SKEW_SECONDS,
        organizations: str | Sequence[str] = (),
    ) -> None:
        if not issuer:
            raise ValueError("issuer 不能为空：多用户模式下必须配置 CASDOOR_ISSUER")
        # audience 允许为空 = 「客户端自治」模式（ADR 0008）：只要令牌的 aud 包含
        # 它自己的 client_id（即确实是发给这个客户端的）就放行，租户由 client_id 解析。
        # 配了 MYBMO_OIDC_AUDIENCE 就回到白名单模式，作为额外收紧。
        audiences = _normalize_audiences(audience)
        self._issuer = issuer.rstrip("/")
        self._audiences = audiences
        # 组织白名单：本服务只服务一个 Casdoor 组织（pandabase），
        # 令牌的 owner 声明不在列表里直接拒。空 = 不校验。
        self._organizations = _normalize_audiences(organizations)
        self._clock_skew = clock_skew
        # 默认值只是兜底：实测 issuer 给的是 /.well-known/jwks，不是这里推导的路径。
        # 部署时必须显式设 MYBMO_JWKS_URL，见 docs/auth-contract.md。
        self._jwks = JwksCache(jwks_url or f"{self._issuer}/.well-known/jwks")

    @property
    def issuer(self) -> str:
        return self._issuer

    @property
    def audiences(self) -> tuple[str, ...]:
        """全部被允许的 client_id。"""
        return self._audiences

    @property
    def audience(self) -> str:
        """兼容单值调用方：返回第一个（未配白名单时为空串）。"""
        return self._audiences[0] if self._audiences else ""

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

        decode_kwargs: dict[str, Any] = {
            "algorithms": ["RS256", "ES256"],
            "issuer": self._issuer,
            "leeway": self._clock_skew,
            "options": {
                "require": ["iss", "aud", "exp", "sub"],
                "verify_iss": True,
                # 白名单模式下由 PyJWT 精确比对；自治模式下关了，改由下面手工判定
                "verify_aud": bool(self._audiences),
            },
        }
        if self._audiences:
            decode_kwargs["audience"] = list(self._audiences)
        try:
            claims = jwt.decode(token, signing_key.key, **decode_kwargs)
        except jwt.ExpiredSignatureError as exc:
            raise TokenError("令牌已过期，请重新登录") from exc
        except jwt.InvalidAudienceError as exc:
            allowed = "、".join(self._audiences)
            raise TokenError(
                f"令牌的 audience 不在允许的客户端列表里（允许 {allowed}）。"
                "Casdoor 把 aud 填成 client_id；这批 client_id 由 MYBMO_OIDC_AUDIENCE 收紧，"
                "见 docs/auth-contract.md"
            ) from exc
        except jwt.InvalidIssuerError as exc:
            raise TokenError(f"令牌的 issuer 不受信任（期望 {self._issuer}）") from exc
        except jwt.InvalidTokenError as exc:
            raise TokenError(f"令牌无效：{exc}") from exc

        if self._organizations:
            owner = str(claims.get("owner") or claims.get("organization") or "").strip()
            if owner not in self._organizations:
                allowed = "、".join(self._organizations)
                raise TokenError(
                    f"令牌所属组织为 {owner or '未标注'}，而本服务只接受 {allowed} 组织的令牌。"
                    "组织与服务的对应关系见 docs/auth-contract.md"
                )

        client_id = str(claims.get("client_id") or claims.get("azp") or "").strip()
        if not self._audiences:
            # 自治模式：令牌必须是**发给这个客户端自己的**，否则就是拿别人资源的令牌来用
            if not client_id:
                raise TokenError(
                    "令牌既不在白名单模式下校验，也没有 client_id（azp），无法判断来源"
                )
            aud = claims.get("aud")
            aud_list = [aud] if isinstance(aud, str) else list(aud or [])
            if aud_list and client_id not in [str(a) for a in aud_list]:
                raise TokenError(
                    f"令牌的 aud（{aud_list}）不包含它自己的 client_id（{client_id}）。"
                    "常见原因：客户端带了 RFC 8707 的 resource 参数，Casdoor 会把 aud "
                    "换成资源地址——去掉 resource 即可"
                )

        scope_text = str(claims.get("scope") or claims.get("scp") or "")
        scopes = tuple(sorted({s for s in scope_text.replace(",", " ").split() if s}))

        return VerifiedToken(
            subject=str(claims["sub"]),
            client_id=client_id,
            audience=(
                _matched_audience(claims.get("aud"), self._audiences)
                if self._audiences
                else client_id
            ),
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
            # IdP 没有 RFC 8707 的资源标识，aud 就是 client_id，
            # 这里如实回填命中的那一个，不用资源地址冒充
            resource=verified.audience,
            subject=verified.subject,
            claims=verified.claims,
        )
