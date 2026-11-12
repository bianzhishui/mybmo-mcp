"""JSONL 审计后端：一行一条 JSON，按天分文件（``data/audit/2026-11-07.jsonl``）。

不做轮转，清理属运维动作（按保留期删文件即可）。字段定义见
:mod:`mybmo_mcp.audit.base`——那边是与 PG 后端共用的契约。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from mybmo_mcp.audit.base import (  # noqa: F401  (对外再导出，兼容既有导入)
    STATE_DENIED,
    STATE_FAILED,
    STATE_SUCCEEDED,
    STATE_UNKNOWN,
    STATES,
    AuditRecord,
    build_record,
    new_call_id,
    payload_digest,
)

logger = logging.getLogger(__name__)


class FileAuditSink:
    """JSONL 写入器。``directory=None`` 时只构造记录、不落盘（测试用）。"""

    def __init__(self, directory: str | Path | None, *, store_payload: bool = True) -> None:
        self._directory = Path(directory) if directory is not None else None
        self._store_payload = store_payload
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
        user_name: str = "",
        app: str = "",
        account_name: str = "",
        started_at: float = 0.0,
        finished_at: float = 0.0,
        http_status: int | None = None,
        response_body: Any = None,
    ) -> AuditRecord:
        record = build_record(
            sub=sub,
            tenant_id=tenant_id,
            account=account,
            capability=capability,
            permission=permission,
            mutating=mutating,
            state=state,
            call_id=call_id,
            duration_ms=duration_ms,
            error_code=error_code,
            error=error,
            payload=payload,
            user_name=user_name,
            app=app,
            account_name=account_name,
            started_at=started_at,
            finished_at=finished_at,
            http_status=http_status,
            response_body=response_body,
            store_payload=self._store_payload,
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


#: 兼容旧名字（原来只有文件一种实现）
AuditLog = FileAuditSink


def _date(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))
