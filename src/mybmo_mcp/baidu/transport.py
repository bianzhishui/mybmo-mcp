"""百度营销 API 传输层。

唯一知道百度网关长什么样的地方：拼 URL、把鉴权放进 JSON body 的 header、
发请求、解析 ``header.status``、按读写区分重试策略。不含任何业务语义。

三条硬规则（均来自真实账户踩坑）：

1. ``userName`` / ``accessToken`` / ``action`` 必须在请求体 JSON 的 ``header`` 里，
   放 HTTP 头会报 ``8002``。
2. ``userName`` 必须是令牌所属账户的中文名，填错报 ``81020``。
3. 服务名与方法名大小写敏感，``KRService`` 的 ``KR`` 必须全大写，否则 ``87091``。

方法名允许小写蛇形或驼峰输入，由 :func:`normalize_method` 归一化；
已经是正确写法的名字经过归一化后保持不变（幂等），因此可以放心重复调用。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx2

from mybmo_mcp.baidu.errors import BaiduApiError, DryRunRequested

SMS_BASE = "https://api.baidu.com/json/sms/service"
FEED_BASE = "https://api.baidu.com/json/feed/v1"

DEFAULT_TIMEOUT = 45.0
DEFAULT_READ_RETRIES = 2
DEFAULT_MIN_INTERVAL = 0.2
DEFAULT_ACTION = "API-PY-SDK"

#: 读方法前缀。不匹配的一律按写处理——写请求绝不自动重试。
READ_PREFIXES = ("get", "query", "list", "search", "describe", "download", "check", "count")


@dataclass(frozen=True, slots=True)
class Exchange:
    """最近一次 HTTP 交互的快照，供审计落库用。

    传输层原本把 ``status_code`` 和原始响应都吞掉了，审计只能记"成功/失败"这种
    粗粒度结论。有了这个快照，审计能记下真正的 HTTP 状态码与出参原文。
    """

    url: str
    http_status: int | None
    response_text: str
    started_at: float
    finished_at: float

    @property
    def duration_ms(self) -> int:
        return max(0, int((self.finished_at - self.started_at) * 1000))


@dataclass(frozen=True, slots=True)
class RequestCredentials:
    """一次请求所需的鉴权信息。

    ``access_token`` 不进 ``repr``，避免异常栈或调试输出把它带出去。
    """

    user_name: str
    access_token: str

    def __repr__(self) -> str:
        return f"RequestCredentials(user_name={self.user_name!r}, access_token='***')"


@runtime_checkable
class CredentialSource(Protocol):
    """凭据来源。P2 的凭据库实现这个协议，刷新逻辑对传输层透明。"""

    async def credentials(self) -> RequestCredentials: ...


class StaticCredentials:
    """固定凭据。P1 与测试用；P2 换成带刷新与加锁的实现。"""

    def __init__(self, creds: RequestCredentials) -> None:
        self._creds = creds

    async def credentials(self) -> RequestCredentials:
        return self._creds


def normalize_service(name: str) -> str:
    """``accountService`` -> ``AccountService``；``krService`` -> ``KRService``。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("service 不能为空")
    return (name[0].upper() + name[1:]).replace("Kr", "KR")


def normalize_method(name: str) -> str:
    """``get_account_info`` / ``getAccountInfo`` -> ``getAccountInfo``；``KR`` 强制大写。

    对已经规范的名字幂等。
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("method 不能为空")
    if "_" in name:
        parts = [p for p in name.split("_") if p]
        name = parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])
    else:
        name = name[0].lower() + name[1:]
    return name.replace("Kr", "KR")


def build_url(service: str, method: str, product_line: str = "sms") -> str:
    base = FEED_BASE if product_line == "feed" else SMS_BASE
    return f"{base}/{service}/{method}"


def is_write_method(method: str) -> bool:
    """按方法名前缀判定读写。判定偏保守：认不出来的一律当写。"""
    name = (method or "").strip().lstrip("_").lower()
    return not name.startswith(READ_PREFIXES)


def build_payload(
    creds: RequestCredentials, body: dict[str, Any] | None = None, *, action: str = DEFAULT_ACTION
) -> dict[str, Any]:
    """构造百度网关要求的请求体。鉴权在 body 里，不在 HTTP 头里。"""
    return {
        "header": {
            "userName": creds.user_name,
            "accessToken": creds.access_token,
            "action": action,
        },
        "body": body if body is not None else {},
    }


class BaiduTransport:
    """异步传输层。一个实例对应一个账户，并发安全（内部状态只用于限速与配额）。"""

    def __init__(
        self,
        credentials: CredentialSource,
        *,
        client: httpx2.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        read_retries: int = DEFAULT_READ_RETRIES,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        backoff_base: float = 1.0,
    ) -> None:
        self._credentials = credentials
        self._client = client if client is not None else httpx2.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._read_retries = max(0, read_retries)
        self._min_interval = max(0.0, min_interval)
        self._backoff = max(0.0, backoff_base)
        self._last_call = 0.0
        #: 最近一次响应的配额信息，由调用方按需读取
        self.last_rquota: dict[str, Any] | None = None
        #: 最近一次 HTTP 交互快照（审计用）。没发出去（越权/参数错）时保持 ``None``
        self.exchange: Exchange | None = None

    async def call(
        self,
        service: str,
        method: str,
        body: dict[str, Any] | None = None,
        *,
        product_line: str = "sms",
        dry_run: bool = False,
    ) -> Any:
        """调用任意百度营销接口，返回响应里的 ``body.data``。

        失败一律抛 :class:`BaiduApiError`，由调用方按 ``outcome`` 决定是否重试。
        """
        service = normalize_service(service)
        method = normalize_method(method)
        url = build_url(service, method, product_line)
        creds = await self._credentials.credentials()
        payload = build_payload(creds, body)

        if dry_run:
            raise DryRunRequested(url, payload)

        await self._throttle()
        return await self._send(url, payload, service, method)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> BaiduTransport:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        delta = time.monotonic() - self._last_call
        if delta < self._min_interval:
            await asyncio.sleep(self._min_interval - delta)
        self._last_call = time.monotonic()

    async def _send(self, url: str, payload: dict[str, Any], service: str, method: str) -> Any:
        write = is_write_method(method)
        retries = 0 if write else self._read_retries
        started = time.time()

        for attempt in range(retries + 1):
            try:
                response = await self._client.post(url, json=payload)
            except httpx2.TimeoutException as exc:
                # 超时属于「结果未知」：读可以重试，写绝不能重发。
                self.exchange = Exchange(url, None, "", started, time.time())
                if attempt < retries:
                    await asyncio.sleep(self._backoff * (2**attempt))
                    continue
                raise BaiduApiError(
                    f"{service}/{method} 请求超时，结果未知",
                    service=service,
                    method=method,
                    outcome="unknown",
                ) from exc
            except httpx2.HTTPError as exc:
                # 连接层失败（DNS、TLS、连接重置）。写请求同样不重试。
                self.exchange = Exchange(url, None, "", started, time.time())
                if attempt < retries:
                    await asyncio.sleep(self._backoff * (2**attempt))
                    continue
                raise BaiduApiError(
                    f"{service}/{method} 网络失败，结果未知：{type(exc).__name__}",
                    service=service,
                    method=method,
                    outcome="unknown",
                ) from exc

            self.exchange = Exchange(
                url, response.status_code, response.text, started, time.time()
            )

            if response.status_code >= 500:
                # 网关侧异常，不重试：写请求可能已被接受，读请求下一轮由调用方决定。
                raise BaiduApiError(
                    f"{service}/{method} HTTP {response.status_code}，结果未知",
                    service=service,
                    method=method,
                    outcome="unknown",
                    http_status=response.status_code,
                )
            if response.status_code >= 400:
                raise BaiduApiError(
                    f"{service}/{method} HTTP {response.status_code}",
                    service=service,
                    method=method,
                    outcome="failed",
                    http_status=response.status_code,
                )
            try:
                return self._parse(response.text, service, method)
            except BaiduApiError as exc:
                # 业务错误码也带上 HTTP 状态，审计才分得清"网关挂了"和"参数不对"
                if exc.http_status is None:
                    exc.http_status = response.status_code
                raise

        raise BaiduApiError(  # pragma: no cover - 循环必然 return 或 raise
            f"{service}/{method} 重试耗尽",
            service=service,
            method=method,
            outcome="unknown",
        )

    def _parse(self, raw: str, service: str, method: str) -> Any:
        try:
            result = json.loads(raw)
        except ValueError as exc:
            raise BaiduApiError(
                f"{service}/{method} 返回非 JSON，结果未知",
                service=service,
                method=method,
                outcome="unknown",
            ) from exc

        if not isinstance(result, dict):
            raise BaiduApiError(
                f"{service}/{method} 响应顶层不是对象，结果未知",
                service=service,
                method=method,
                outcome="unknown",
            )

        header = result.get("header")
        if not isinstance(header, dict) or "status" not in header:
            raise BaiduApiError(
                f"{service}/{method} 响应缺少有效 header.status，结果未知",
                service=service,
                method=method,
                outcome="unknown",
            )

        rquota = header.get("rquota")
        self.last_rquota = rquota if isinstance(rquota, dict) else None

        status = header.get("status")
        if status != 0:
            failures = header.get("failures")
            first = failures[0] if isinstance(failures, list) and failures else None
            first = first if isinstance(first, dict) else {}
            raise BaiduApiError(
                str(first.get("message") or header.get("desc") or f"status={status}"),
                code=first.get("code"),
                position=first.get("position"),
                service=service,
                method=method,
            )

        body = result.get("body")
        if not isinstance(body, dict):
            raise BaiduApiError(
                f"{service}/{method} 成功响应的 body 不是对象，结果未知",
                service=service,
                method=method,
                outcome="unknown",
            )
        return body.get("data")
