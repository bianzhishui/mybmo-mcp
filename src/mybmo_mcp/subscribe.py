"""百度订阅（事件推送）的接收端。

只做三件事：**验共享密钥 → 把报文原样记下来 → 回 200**。

为什么只"打印"不"处理"：订阅的事件类型、报文格式、签名算法都还没有实测依据
（``docs/baidu-behavior.md`` 里没有结论），所以这个端点先当**黑盒采样器**用——
攒够真实样本再写解析器。而且 ADR 0003 说得很清楚：能力层不做业务编排，
把外部推送直接翻译成"暂停计划""调预算"这类动作，等于给公网开了一个写入口。

三条纪律：

1. **不配密钥就 503**（失败关闭）。宁可这个端点不可用，也不能悄悄变成匿名接收器。
2. **验签失败不落盘**，只记一行告警（含来源 IP 与命中的密钥位置），避免被扫描器灌爆磁盘。
3. **入库前脱敏**：把配置的密钥值从头、查询串、body 里抹掉。原始报文要留下给人看，
   密钥不要留下。
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from mybmo_mcp.baidu.errors import CredentialError

#: 一次推送最多读这么多，超出截断并标注 ``body_truncated``。
MAX_BODY_BYTES = 64 * 1024


#: 密钥可能放在哪。百度侧到底怎么传**未实测**，所以这里多认几个位置，
#: 并记录命中位置——看过几种真实请求后就能收敛成一个。
BEARER_PREFIX = "bearer "
KEY_HEADERS: tuple[str, ...] = (
    "x-mybmo-token",
    "x-subscribe-token",
    "x-baidu-token",
    "x-auth-token",
    "x-api-key",
)
KEY_QUERY_KEYS: tuple[str, ...] = ("token", "key", "secret", "signature")

_REDACTED = "***"

#: 百度控制台对订阅密钥的格式要求：**32 位，仅小写字母与数字**。
#: 填别的格式在控制台就过不去，所以这里做一次形状校验，配错了在启动日志里就能看到。
KEY_LEN = 32
KEY_ALPHABET = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


def looks_like_baidu_key(value: str) -> bool:
    """是否符合百度订阅密钥的形状（32 位小写字母/数字）。

    只用来提前告警，**不用来收紧校验**：万一百度放宽或换规则，
    我们不该因此把真实推送拒掉。
    """
    return len(value) == KEY_LEN and set(value) <= KEY_ALPHABET


def extract_key(
    headers: Mapping[str, str], params: Mapping[str, str]
) -> tuple[str, str]:
    """找出请求里的密钥。返回 ``(值, 来源描述)``；没有则 ``("", "")``。"""
    authorization = headers.get("authorization", "").strip()
    if authorization.lower().startswith(BEARER_PREFIX):
        return authorization[len(BEARER_PREFIX) :].strip(), "authorization:bearer"

    for name in KEY_HEADERS:
        value = headers.get(name, "").strip()
        if value:
            return value, f"header:{name}"

    for name in KEY_QUERY_KEYS:
        value = params.get(name, "").strip()
        if value:
            return value, f"query:{name}"

    return "", ""


def check_key(
    expected: str, headers: Mapping[str, str], params: Mapping[str, str]
) -> tuple[bool, str]:
    """常量时间比较密钥。返回 ``(是否通过, 命中来源)``。"""
    if not expected:
        return False, ""
    presented, source = extract_key(headers, params)
    if not presented:
        return False, ""
    # compare_digest 要求同类型；两侧都是 str 且只含 ASCII 时才是常量时间，
    # 非 ASCII 会抛 TypeError，所以先按 bytes 比。
    try:
        ok = secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
    except TypeError:
        return False, source
    return ok, source


def redact(text: str, secret: str) -> str:
    """把密钥从任意片段里抹掉。空密钥时原样返回。"""
    if not secret:
        return text
    return text.replace(secret, _REDACTED)


def build_record(
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    params: Mapping[str, str],
    body: bytes,
    secret: str,
    key_source: str,
    client: str = "",
    now=time.time,
) -> dict[str, Any]:
    """把一次推送整理成可落盘的记录。只脱敏密钥，其余原样保留。"""
    raw = body.decode("utf-8", errors="replace")
    truncated = len(body) > MAX_BODY_BYTES
    if truncated:
        raw = raw[:MAX_BODY_BYTES]
    redacted = redact(raw, secret)
    # 常见是 JSON：解析一份放进结构里方便肉眼比对字段名；解析不出来就只留原文
    parsed: Any = None
    if redacted.strip().startswith(("{", "[")):
        try:
            parsed = json.loads(redacted)
        except ValueError:
            parsed = None

    return {
        "received_at": now(),
        "method": method,
        "path": path,
        "client": client,
        "key_source": key_source,
        "query": {k: redact(str(v), secret) for k, v in params.items()},
        "headers": {k: redact(str(v), secret) for k, v in headers.items()},
        "body": redacted,
        "body_truncated": truncated,
        "body_json": parsed,
    }


class PgSubscribeStore:
    """把推送样本写进 ``subscribe_events`` 表（ADR 0009 D6）。

    端点只采样、不解析（ADR 0003），所以这里也不需要事务语义：
    单条 INSERT，失败就记日志——百度重推一遍也不会更好。
    """

    def __init__(self, dsn: str, *, connect: Any = None) -> None:
        if not dsn:
            raise CredentialError("PgSubscribeStore 需要 MYBMO_DATABASE_URL")
        self._dsn = dsn
        self._connect_override = connect

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        if self._connect_override is not None:
            conn = self._connect_override()
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()
            return

        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            yield conn

    def save(self, record: Mapping[str, Any]) -> int:
        """写入一条样本，返回行 id。"""
        with self._conn() as conn:
            row = conn.execute(
                "INSERT INTO subscribe_events"
                " (received_at, method, path, client, key_source, query, headers,"
                "  body, body_truncated, body_json)"
                " VALUES (to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " RETURNING id",
                (
                    float(record.get("received_at") or time.time()),
                    str(record.get("method") or ""),
                    str(record.get("path") or ""),
                    str(record.get("client") or ""),
                    str(record.get("key_source") or ""),
                    json.dumps(record.get("query") or {}, ensure_ascii=False),
                    json.dumps(record.get("headers") or {}, ensure_ascii=False),
                    str(record.get("body") or ""),
                    bool(record.get("body_truncated")),
                    None
                    if record.get("body_json") is None
                    else json.dumps(record["body_json"], ensure_ascii=False),
                ),
            ).fetchone()
        return int(row["id"])
