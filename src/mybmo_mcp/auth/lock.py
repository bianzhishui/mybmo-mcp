"""账户级互斥锁：保证同一账户的令牌刷新串行执行。

refreshToken 刷新会**轮换**：两个请求同时刷新，后到的那个拿着已失效的旧令牌，
账户直接掉线。这道锁就是防这个。

实现用 ``flock``：锁的是打开的文件描述而不是进程，所以同一台机器上跨进程有效。
多副本部署时改用 PostgreSQL advisory lock（见 ADR 0002 的 D4）。
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from mybmo_mcp.baidu.errors import CredentialError

DEFAULT_TIMEOUT = 30.0
DEFAULT_POLL = 0.05


@asynccontextmanager
async def exclusive(
    path: str | os.PathLike[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    poll: float = DEFAULT_POLL,
) -> AsyncIterator[None]:
    """获取排他锁。锁被占用时轮询等待，不阻塞事件循环。"""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CredentialError(
                        f"等待账户锁超时（{timeout:.0f}s）：{lock_path.name}"
                    ) from None
                await asyncio.sleep(poll)
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - 解锁失败不影响正确性
                pass
        os.close(fd)
