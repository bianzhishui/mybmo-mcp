from __future__ import annotations

import pytest

from mybmo_mcp import __version__
from mybmo_mcp.server import main


def test_package_imports() -> None:
    assert __version__


def test_help_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0

    out = capsys.readouterr().out
    assert "--transport" in out
    assert "stdio" in out
    assert "streamable-http" in out
