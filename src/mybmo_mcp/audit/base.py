"""审计记录的公共定义：数据结构、状态常量、写入协议。

两条实现共用这里，调用方只依赖 ``AuditSink`` 协议：

* :class:`~mybmo_mcp.audit.log.FileAuditSink` —— JSONL，按天分文件（默认）
* :class:`~mybmo_mcp.audit.pg.PgAuditSink` —— PostgreSQL ``audit_log`` 表

五条规矩（对两种后端都成立）：

1. **失败也留痕**。成功、业务失败、结果未知、越权拒绝各是一个状态，
   ``unknown`` 尤其重要——它是"不知道远端做没做"的唯一记录。
2. **不记令牌**。错误信息统一过 :func:`~mybmo_mcp.baidu.errors.redact`。
3. **写日志失败不能拖垮调用**。让所有查询因为审计写不进去而失败，代价更大。
4. **报文按需保存**。``store_payload`` 关掉时只留 ``payload_digest``。
5. **审计是旁路**，不参与业务事务。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from mybmo_mcp.baidu.errors import redact

STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_UNKNOWN = "unknown"
STATE_DENIED = "denied"

#: 四态 + 越权拒绝。``denied`` 单列，因为它不是"失败"——请求根本没发出去。
STATES = (STATE_SUCCEEDED, STATE_FAILED, STATE_UNKNOWN, STATE_DENIED)


def new_call_id() -> str:
    """本地调用 ID，用来把日志行和返回给调用方的 ``meta.call_id`` 对上。

    百度响应里有没有稳定的请求 ID 尚未实证，所以不自造上游 ID，
    只给一个本地可引用的编号。
    """
    return uuid.uuid4().hex[:12]


def payload_digest(payload: Any) -> str:
    """报文摘要。规范化键序后取 sha256 前 16 位。"""
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """一次调用的审计事实。

    ``request_params`` / ``response_body`` 只在 ``store_payload`` 打开时填充；
    关掉时两者为 ``None``，靠 ``payload_digest`` 比对。
    """

    ts: float
    call_id: str
    sub: str
    tenant_id: str
    account: str
    capability: str
    permission: str
    mutating: str
    state: str
    duration_ms: int = 0
    error_code: int | None = None
    error: str = ""
    payload_digest: str = ""
    user_name: str = ""
    app: str = ""
    account_name: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    http_status: int | None = None
    request_params: Any = None
    response_body: Any = None
    request_bytes: int = 0
    response_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "call_id": self.call_id,
            "sub": self.sub,
            "tenant_id": self.tenant_id,
            "account": self.account,
            "capability": self.capability,
            "permission": self.permission,
            "mutating": self.mutating,
            "state": self.state,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
            "error": self.error,
            "payload_digest": self.payload_digest,
            "user_name": self.user_name,
            "app": self.app,
            "account_name": self.account_name,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "http_status": self.http_status,
            "request_bytes": self.request_bytes,
            "response_bytes": self.response_bytes,
        }


@runtime_checkable
class AuditSink(Protocol):
    """审计写入协议。实现方**不得抛异常**——写不进去只能记日志。"""

    def record(
        self,
        *,
        sub: str,
        tenant_id: str,
        account: str,
        capability: str,
        permission: str,
        mutating: str,
        state: str,
        call_id: str,
        duration_ms: int = 0,
        error_code: int | None = None,
        error: str = "",
        payload: Any = None,
        user_name: str = "",
        app: str = "",
        account_name: str = "",
        started_at: float = 0.0,
        finished_at: float = 0.0,
        http_status: int | None = None,
        response_body: Any = None,
    ) -> AuditRecord: ...


def build_record(
    *,
    sub: str,
    tenant_id: str,
    account: str,
    capability: str,
    permission: str,
    mutating: str,
    state: str,
    call_id: str,
    duration_ms: int = 0,
    error_code: int | None = None,
    error: str = "",
    payload: Any = None,
    user_name: str = "",
    app: str = "",
    account_name: str = "",
    started_at: float = 0.0,
    finished_at: float = 0.0,
    http_status: int | None = None,
    response_body: Any = None,
    store_payload: bool = True,
) -> AuditRecord:
    """组装审计记录。两种后端共用，避免各写一份字段映射。"""
    now = time.time()
    finished = finished_at or now
    started = started_at or (finished - duration_ms / 1000.0)
    request_bytes = _size_of(payload) if payload is not None else 0
    response_bytes = _size_of(response_body) if response_body is not None else 0
    return AuditRecord(
        ts=now,
        call_id=call_id,
        sub=sub,
        tenant_id=tenant_id,
        account=account,
        capability=capability,
        permission=permission,
        mutating=mutating,
        state=state,
        duration_ms=max(0, int(duration_ms)),
        error_code=error_code,
        error=redact(error) if error else "",
        payload_digest=payload_digest(payload) if payload is not None else "",
        user_name=user_name,
        app=app,
        account_name=account_name,
        started_at=started,
        finished_at=finished,
        http_status=http_status,
        request_params=payload if store_payload else None,
        response_body=response_body if store_payload else None,
        request_bytes=request_bytes,
        response_bytes=response_bytes,
    )


def _size_of(value: Any) -> int:
    """报文序列化后的字节数。序列化失败时退化成 0，不能因此丢掉整条审计。"""
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "STATES",
    "STATE_DENIED",
    "STATE_FAILED",
    "STATE_SUCCEEDED",
    "STATE_UNKNOWN",
    "AuditRecord",
    "AuditSink",
    "build_record",
    "new_call_id",
    "payload_digest",
]
