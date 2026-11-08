"""JWT 校验：四种必须拒绝的路径，以及三条容易被放松的边界。

拒绝路径来自 tasks.md 5.8：伪造签名、错 audience、过期、issuer 不符。
另外三条是本项目特有的：

- issuer 拼写不同（哪怕只差一个末尾斜杠）必须拒——多接受一个 issuer 就是多开后门
- 缺 scope 不是"令牌无效"，而是"权限不足"，两者状态码不同
- JWKS 拉不到时拒绝，不能退化成不验签
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from mybmo_mcp.auth.resource import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    SCOPES_SUPPORTED,
    JwtVerifier,
    TokenError,
)

ISSUER = "https://auth.pandabase.top"
AUDIENCE = "https://baiduads.pandabase.top"
OTHER_ISSUER = "https://auth.evil.example"
OTHER_AUDIENCE = "https://someone-else.example"


# ---------- 密钥与令牌构造 ----------


def make_keypair() -> tuple[Any, dict[str, Any]]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = RSAAlgorithm.to_jwk(private.public_key())
    import json

    jwk = json.loads(public)
    jwk["kid"] = "test-key-1"
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return private, jwk


@pytest.fixture(scope="module")
def keypair() -> tuple[Any, dict[str, Any]]:
    return make_keypair()


@pytest.fixture
def verifier(keypair: tuple[Any, dict[str, Any]], monkeypatch) -> JwtVerifier:
    """校验器，JWKS 用内存里的公钥，不打网络。"""
    private, jwk = keypair
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE)
    monkeypatch.setattr(
        type(verifier._jwks),
        "get",
        lambda self, now=None: _StubJwks(private, jwk),
    )
    return verifier


class _StubJwks:
    """假的 JWKS 客户端：只认一把密钥，行为与 PyJWKClient 一致到够用。"""

    def __init__(self, private: Any, jwk: dict[str, Any]) -> None:
        self._private = private
        self._jwk = jwk

    def get_signing_key_from_jwt(self, token: str) -> Any:
        header = jwt.get_unverified_header(token)
        if header.get("kid") != self._jwk["kid"]:
            raise jwt.PyJWKClientError(f"Unknown kid: {header.get('kid')}")
        return type("Key", (), {"key": self._private.public_key()})()


def make_token(
    keypair: tuple[Any, dict[str, Any]],
    **overrides: Any,
) -> str:
    private, jwk = keypair
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "u_zhang",
        "client_id": "claude-desktop",
        "scope": f"{SCOPE_READ} {SCOPE_WRITE}",
        "iat": now - 30,
        "exp": now + 600,
    }
    claims.update(overrides)
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": jwk["kid"]})


# ---------- 正常路径 ----------


def test_valid_token_is_accepted(
    verifier: JwtVerifier, keypair: tuple[Any, dict[str, Any]]
) -> None:
    verified = verifier.verify(make_token(keypair))

    assert verified.subject == "u_zhang"
    assert verified.client_id == "claude-desktop"
    assert set(verified.scopes) == {SCOPE_READ, SCOPE_WRITE}
    assert verified.has_scope(SCOPE_READ)


# ---------- 四种拒绝路径 ----------


def test_foreign_signature_is_rejected(
    verifier: JwtVerifier, keypair: tuple[Any, dict[str, Any]]
) -> None:
    """用别人的私钥签，但 issuer / aud 都写对——只有验签能拦住这种情况。"""
    other_private, _ = make_keypair()
    now = int(time.time())
    forged = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "u_zhang",
            "exp": now + 600,
            "scope": SCOPE_READ,
        },
        other_private,
        algorithm="RS256",
        headers={"kid": "test-key-1"},
    )

    with pytest.raises(TokenError, match="令牌无效"):
        verifier.verify(forged)


def test_wrong_audience_is_rejected(
    verifier: JwtVerifier, keypair: tuple[Any, dict[str, Any]]
) -> None:
    with pytest.raises(TokenError, match="audience 不是本服务"):
        verifier.verify(make_token(keypair, aud=OTHER_AUDIENCE))


def test_expired_token_is_rejected(
    verifier: JwtVerifier, keypair: tuple[Any, dict[str, Any]]
) -> None:
    past = int(time.time()) - 3600
    with pytest.raises(TokenError, match="已过期"):
        verifier.verify(make_token(keypair, exp=past, iat=past - 60))


def test_foreign_issuer_is_rejected(
    verifier: JwtVerifier, keypair: tuple[Any, dict[str, Any]]
) -> None:
    with pytest.raises(TokenError, match="issuer 不受信任"):
        verifier.verify(make_token(keypair, iss=OTHER_ISSUER))


# ---------- 三条容易放松的边界 ----------


def test_issuer_with_different_trailing_slash_is_rejected(
    verifier: JwtVerifier, keypair: tuple[Any, dict[str, Any]]
) -> None:
    """RFC 8414 要求 issuer 精确比较：带斜杠和不带斜杠是两个不同的 issuer。"""
    with pytest.raises(TokenError, match="issuer 不受信任"):
        verifier.verify(make_token(keypair, iss=ISSUER + "/"))


def test_missing_scope_is_not_a_token_failure(
    verifier: JwtVerifier, keypair: tuple[Any, dict[str, Any]]
) -> None:
    """缺 scope 的令牌是**有效令牌**，只是权限不足。

    校验层不负责判权限——那是工具层按 mutating 判档位的事，
    返回 403 + insufficient_scope 而不是 401。
    """
    verified = verifier.verify(make_token(keypair, scope=""))

    assert verified.scopes == ()
    assert not verified.has_scope(SCOPE_WRITE)


def test_jwks_failure_does_not_fall_back_to_no_verification(
    keypair: tuple[Any, dict[str, Any]], monkeypatch
) -> None:
    """kid 不认识时拒绝。

    最坏的实现是"找不到密钥就跳过验签"，那等于在密钥轮换窗口里接受任何人的令牌。
    这里直接把 JWKS 客户端换成抛错的桩，绕开缓存逻辑，只测"失败会不会变成放行"。
    """

    class UnknownKid:
        def get_signing_key_from_jwt(self, token: str) -> Any:
            raise jwt.PyJWKClientError("Unknown kid")

    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE)
    monkeypatch.setattr(type(verifier._jwks), "get", lambda self, now=None: UnknownKid())

    with pytest.raises(TokenError, match="无法解析令牌签名密钥"):
        verifier.verify(make_token(keypair))


def test_malformed_token_is_a_token_error_not_a_500(verifier: JwtVerifier, monkeypatch) -> None:
    """畸形令牌在取 kid 阶段就抛 DecodeError，早于 decode()。

    不接住它就会穿透成 500——客户端看到"服务器炸了"，而不是"令牌无效"。
    """

    class PlainDecodeError:
        def get_signing_key_from_jwt(self, token: str) -> Any:
            raise jwt.DecodeError("Not enough segments")

    monkeypatch.setattr(type(verifier._jwks), "get", lambda self, now=None: PlainDecodeError())

    with pytest.raises(TokenError, match="令牌格式错误"):
        verifier.verify("not-a-token")


# ---------- SDK 协议入口 ----------


async def test_verify_token_returns_none_not_raises(
    verifier: JwtVerifier, keypair: tuple[Any, dict[str, Any]]
) -> None:
    """SDK 的中间件靠返回值判 401。抛异常会变成 500，客户端看到的是"服务器炸了"。"""
    bad = await verifier.verify_token("not-a-token")

    assert bad is None


async def test_verify_token_returns_access_token(
    verifier: JwtVerifier, keypair: tuple[Any, dict[str, Any]]
) -> None:
    access = await verifier.verify_token(make_token(keypair))

    assert access is not None
    assert access.subject == "u_zhang"
    assert access.resource == AUDIENCE
    assert SCOPE_READ in access.scopes


# ---------- 构造约束 ----------


def test_empty_issuer_is_refused() -> None:
    with pytest.raises(ValueError, match="issuer 不能为空"):
        JwtVerifier(issuer="", audience=AUDIENCE)


def test_empty_audience_is_refused() -> None:
    with pytest.raises(ValueError, match="audience 不能为空"):
        JwtVerifier(issuer=ISSUER, audience="")


def test_scopes_are_the_three_documented() -> None:
    assert SCOPES_SUPPORTED == (SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)
