"""Tests for staging the Yahoo Chrome extension."""

from __future__ import annotations

from pathlib import Path

from analyst_ledger.install_extension import EXTENSION_FILES, stage_yahoo_extension


def test_stage_yahoo_extension(tmp_path: Path) -> None:
    dest = tmp_path / "Yahoo Capture Extension"
    out = stage_yahoo_extension(dest=dest)
    assert out == dest.resolve()
    for name in EXTENSION_FILES:
        assert (out / name).is_file()
    assert (out / "HOW TO INSTALL.txt").is_file()
    assert "Load unpacked" in (out / "HOW TO INSTALL.txt").read_text(encoding="utf-8")
