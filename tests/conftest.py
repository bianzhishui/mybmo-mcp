"""测试会话的临时目录兜底。

部分沙箱与 CI 环境里，pytest 默认的 ``pytest-of-<user>`` 基础目录创建会失败
（父目录已存在但不可写，或文件系统代理拒绝 mkdir），表现为一片 ``FileExistsError``
而不是断言失败。显式指定 basetemp 可以绕开 pytest 的编号目录逻辑。

只有在用户没有显式传 ``--basetemp`` 时才生效。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    if config.option.basetemp:
        return
    candidate = Path(tempfile.gettempdir()) / "mybmo-mcp-pytest"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    config.option.basetemp = str(candidate)
