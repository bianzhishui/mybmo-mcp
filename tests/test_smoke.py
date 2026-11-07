from __future__ import annotations

import mybmo_mcp


def test_package_imports() -> None:
    assert callable(mybmo_mcp.main)
