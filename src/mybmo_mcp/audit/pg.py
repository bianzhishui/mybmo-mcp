# ruff: noqa: S608
# 本模块的 SQL 只拼**固定的列名与运算符**，所有取值一律走 %s 参数化占位符；
# 没有把用户输入拼进 SQL 的路径（bandit 无法区分这两种情况，故整体豁免）。
"""PostgreSQL 审计后端。

表结构见 ``migrations/002_audit_log.sql``；字段契约见 :mod:`mybmo_mcp.audit.base`。

三条实现约束：

1. **绝不抛异常**。审计写不进去只能打日志——否则一次只读查询会因为数据库抖动而失败。
2. **短超时**。连接 3 秒、语句 2 秒；PG 慢的时候宁可丢一条日志也不拖住业务请求。
3. **连接复用**。单连接 + 线程锁，异常后重建。审计是旁路，不值得为它上连接池。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from mybmo_mcp.audit.base import AuditRecord, build_record

logger = logging.getLogger(__name__)

_INSERT = """
INSERT INTO audit_log (
    call_id, started_at, finished_at, duration_ms, tenant_id, user_sub, user_name,
    app, account_alias, account_name, capability, state, http_status, error_code,
    error, request_params, response_body, request_bytes, response_bytes, payload_digest
) VALUES (
    %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s,
    %s, %s::jsonb, %s::jsonb, %s, %s, %s
)
"""


class PgAuditSink:
    """写 ``audit_log`` 表。``connect=None`` 时自己建连接（测试可注入）。"""

    def __init__(
        self,
        dsn: str,
        *,
        store_payload: bool = True,
        connect: Callable[[], Any] | None = None,
        connect_timeout: int = 3,
        statement_timeout_ms: int = 2000,
    ) -> None:
        self._dsn = dsn
        self._store_payload = store_payload
        self._connect_override = connect
        self._connect_timeout = connect_timeout
        self._statement_timeout_ms = statement_timeout_ms
        self._lock = threading.Lock()
        self._conn: Any = None

    # ---------- 连接 ----------

    def _open(self) -> Any:
        if self._connect_override is not None:
            return self._connect_override()
        import psycopg

        return psycopg.connect(
            self._dsn,
            connect_timeout=self._connect_timeout,
            options=f"-c statement_timeout={self._statement_timeout_ms}",
        )

    def _connection(self) -> Any:
        if self._conn is None or getattr(self._conn, "closed", False):
            self._conn = self._open()
        return self._conn

    def _reset(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception as exc:
                logger.debug("关闭审计连接失败（忽略）：%s", exc)

    # ---------- 写入 ----------

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
        self._insert(record)
        return record

    def _insert(self, record: AuditRecord) -> None:
        params = (
            json.dumps(record.request_params, ensure_ascii=False)
            if record.request_params is not None
            else None
        )
        body = (
            json.dumps(record.response_body, ensure_ascii=False)
            if record.response_body is not None
            else None
        )
        args = (
            record.call_id,
            _ts(record.started_at),
            _ts(record.finished_at),
            record.duration_ms,
            record.tenant_id,
            record.sub,
            record.user_name,
            record.app,
            record.account,
            record.account_name,
            record.capability,
            record.state,
            record.http_status,
            record.error_code,
            record.error,
            params,
            body,
            record.request_bytes,
            record.response_bytes,
            record.payload_digest,
        )
        try:
            with self._lock:
                conn = self._connection()
                with conn.cursor() as cur:
                    cur.execute(_INSERT, args)
                conn.commit()
        except Exception as exc:
            logger.error("AUDIT WRITE FAILED (pg): %s", exc)
            self._reset()


    # ---------- 查询 ----------

    def query(
        self,
        *,
        tenant_id: str,
        visible_subs: list[str] | None = None,
        visible_aliases: list[str] | None = None,
        started_after: str | None = None,
        started_before: str | None = None,
        account: str | None = None,
        capability: str | None = None,
        state: str | None = None,
        http_status: int | None = None,
        error_code: int | None = None,
        call_id: str | None = None,
        user_sub: str | None = None,
        params_match: dict[str, Any] | None = None,
        include_payload: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[dict[str, Any]]]:
        """按条件查审计。**所有条件都带 tenant_id**，调用方负责把可见范围算好。"""
        where = ["tenant_id = %s"]
        args: list[Any] = [tenant_id]

        if visible_subs is not None or visible_aliases is not None:
            parts = []
            if visible_subs:
                parts.append("user_sub = ANY(%s)")
                args.append(list(visible_subs))
            if visible_aliases:
                parts.append("account_alias = ANY(%s)")
                args.append(list(visible_aliases))
            if parts:
                where.append("(" + " OR ".join(parts) + ")")
            else:
                # 既没有别人的授权、也没有自己账户 -> 什么都不该看见
                where.append("false")

        for column, value in (
            ("started_at >=", started_after),
            ("started_at <", started_before),
        ):
            if value:
                where.append(f"{column} %s")
                args.append(value)

        if account:
            where.append("account_alias = %s")
            args.append(account)
        if capability:
            where.append("capability LIKE %s")
            args.append(f"{capability}%")
        if state:
            where.append("state = %s")
            args.append(state)
        if http_status is not None:
            where.append("http_status = %s")
            args.append(http_status)
        if error_code is not None:
            where.append("error_code = %s")
            args.append(error_code)
        if call_id:
            where.append("call_id = %s")
            args.append(call_id)
        if user_sub:
            where.append("user_sub = %s")
            args.append(user_sub)
        if params_match:
            where.append("request_params @> %s::jsonb")
            args.append(json.dumps(params_match, ensure_ascii=False))

        # 拼接的只有**固定的列名与运算符**，所有取值都走 %s 占位符（下面的 args），
        # 不存在把用户输入拼进 SQL 的路径。
        clause = " AND ".join(where)
        payload_cols = ", request_params, response_body" if include_payload else ""
        sql = (
            "SELECT id, call_id, started_at, finished_at, duration_ms, tenant_id, user_sub,"
            " user_name, app, account_alias, account_name, capability, state, http_status,"
            " error_code, error, request_bytes, response_bytes, payload_digest"
            f"{payload_cols}"
            f" FROM audit_log WHERE {clause} ORDER BY started_at DESC, id DESC"
            " LIMIT %s OFFSET %s"
        )
        with self._lock:
            conn = self._connection()
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT count(*) AS n FROM audit_log WHERE {clause}", tuple(args)
                )
                total = _scalar(cur.fetchone())
                cur.execute(sql, (*tuple(args), limit, offset))
                rows = cur.fetchall()
                cols = [d[0] for d in (cur.description or [])]
            conn.commit()
        return total, [_as_dict(row, cols) for row in rows]


def _as_dict(row: Any, cols: list[str]) -> dict[str, Any]:
    """把一行统一成 dict。

    连接上有没有 ``row_factory`` 取决于调用方（生产走 dict_row，测试可能注入默认元组），
    所以这里两种都要支持——元组用游标的列名 zip 回去。
    """
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}  # type: ignore[union-attr]
    return dict(zip(cols, row, strict=False))


def _scalar(row: Any) -> int:
    """``row_factory`` 可能是 dict_row 也可能是默认元组，两种都取到值。"""
    if isinstance(row, dict):
        return int(next(iter(row.values())))
    return int(row[0])


def _ts(value: float) -> datetime | None:
    if not value:
        return None
    return datetime.fromtimestamp(value, tz=UTC)
