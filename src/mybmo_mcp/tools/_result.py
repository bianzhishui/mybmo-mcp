"""工具返回值封装。

对外统一两种形状，模型不需要猜：

- 成功 ``{"ok": true, "data": ..., "meta": {...}}``
- 失败 ``{"ok": false, "error": {"code", "message", "hint"}}``

不用抛异常给 MCP 层，是因为异常在客户端渲染成一段栈或一句 "tool failed"，
而这里能把「错误码 + 该怎么修」直接写在结果里。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mybmo_mcp.baidu.errors import BaiduError


def ok(data: Any, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": True, "data": data, "meta": meta or {}}


def fail(
    message: str,
    *,
    code: int | None = None,
    hint: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message, "hint": hint}
    if extra:
        error.update(extra)
    return {"ok": False, "error": error}


def fail_from(exc: BaseException) -> dict[str, Any]:
    """把已知异常翻译成结构化失败。未知异常交给上层，不在这里吞掉。"""
    if isinstance(exc, BaiduError):
        return fail(
            str(exc),
            code=getattr(exc, "code", None),
            hint=getattr(exc, "hint", None),
            extra={"outcome": getattr(exc, "outcome", None)},
        )
    if isinstance(exc, ValueError):
        return fail(str(exc))
    raise exc


async def guarded(factory: Callable[[], Awaitable[Any]]) -> dict[str, Any]:
    """执行一个异步操作并套上统一信封。"""
    try:
        return ok(await factory())
    except (BaiduError, ValueError) as exc:
        return fail_from(exc)
