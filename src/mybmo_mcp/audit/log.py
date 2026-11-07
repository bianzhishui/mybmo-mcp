"""审计日志：谁、在哪个账户、对哪个能力、结果如何。

没有写审批之后，这份日志是唯一能回答"谁在 3 点 20 分删了这个计划"的东西，
所以它记的是**动作与结果**，不是调试信息。

五条规矩：

1. **一行一条 JSON**，按天分文件（``data/audit/2026-11-07.jsonl``）。不做轮转，
   清理属运维动作，按保留期删文件即可。
2. **不记报文**。只记 ``payload_digest``——同一个报文两次调用摘要相同，
   报文被改过就不同，能对比又不会把投放参数灌进日志。
3. **不记令牌**。错误信息统一过 :func:`~mybmo_mcp.baidu.errors.redact`，
   凭据片段变成 ``***``。
4. **失败也留痕**。成功、业务失败、结果未知、越权拒绝各是一个状态，
   ``unknown`` 尤其重要——它是"不知道远端做没做"的唯一记录。
5. **写日志失败不能拖垮调用**。磁盘满时向 stderr 打一条 ``AUDIT WRITE FAILED``
   并继续，因为让所有查询因为审计写不进去而失败，代价更大。这条是取舍，不是疏忽。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mybmo_mcp.baidu.errors import redact

logger = logging.getLogger(__name__)

STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_UNKNOWN = "unknown"
STATE_DENIED = "denied"

#: 四态 + 越权拒绝。``denied`` 单列，因为它不是"失败"——请求根本没发出去。
STATES = (STATE_SUCCEEDED, STATE_FAILED, STATE_UNKNOWN, STATE_DENIED)


def new_call_id() -> str:
    """本地调用 ID，用来把日志行和返回给调用方的 ``meta.call_id`` 对上。

    百度响应里有没有稳定的请求 ID 尚未实证（tasks 9.x），所以不自造上游 ID，
    只给一个本地可引用的编号。
    """
    return uuid.uuid4().hex[:12]


def payload_digest(payload: Any) -> str:
    """报文摘要。规范化键序后取 sha256 前 16 位。"""
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class AuditRecord:
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
        }


class AuditLog:
    """JSONL 审计写入器。``directory=None`` 时只构造记录、不落盘（测试用）。"""

    def __init__(self, directory: str | Path | None) -> None:
        self._directory = Path(directory) if directory is not None else None
        self._lock = threading.Lock()

    @property
    def directory(self) -> Path | None:
        return self._directory

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
    ) -> AuditRecord:
        record = AuditRecord(
            ts=time.time(),
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
        )
        self._append(record)
        return record

    # ---------- 写入 ----------

    def _append(self, record: AuditRecord) -> None:
        if self._directory is None:
            return
        line = json.dumps(record.to_dict(), ensure_ascii=False) + "\n"
        try:
            with self._lock:
                self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = self._directory / f"{_date(record.ts)}.jsonl"
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
        except OSError as exc:
            # 审计写不进去不是拒绝服务的理由，但必须让人看得见。
            logger.error("AUDIT WRITE FAILED %s: %s", self._directory, exc)


def _date(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))
