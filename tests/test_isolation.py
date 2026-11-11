"""隔离约束的静态检查：仓储层的每条 SQL 都必须带 tenant_id。

多租户最容易出的事故是"某条查询忘了加租户条件"，而这种 bug 在测试里
很难被随机覆盖到。这里直接扫源码：凡是含 SELECT 的 SQL 字面量，
必须同时出现 tenant_id。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PG_STORE = ROOT / "src" / "mybmo_mcp" / "auth" / "pg_tenancy.py"


def sql_literals() -> list[str]:
    tree = ast.parse(PG_STORE.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "SELECT" in node.value.upper():
                found.append(node.value)
    return found


def test_every_select_carries_tenant_id() -> None:
    statements = sql_literals()

    assert statements, "没有扫到任何 SQL，检查提取逻辑"
    offenders = [s for s in statements if "tenant_id" not in s]
    assert not offenders, "以下查询缺少 tenant_id 条件：\n" + "\n---\n".join(offenders)


def test_writes_scope_tenant_too() -> None:
    """INSERT/UPDATE/DELETE 也必须落在本租户内。"""
    tree = ast.parse(PG_STORE.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        text = node.value.upper()
        if not any(k in text for k in ("INSERT INTO", "UPDATE ", "DELETE FROM")):
            continue
        # tenants 表本身就是租户，不需要再带条件
        if "INSERT INTO TENANTS" in text:
            continue
        if "tenant_id" not in node.value:
            offenders.append(node.value)

    assert not offenders, "以下写操作缺少 tenant_id：\n" + "\n---\n".join(offenders)
