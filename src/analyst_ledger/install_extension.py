"""Stage the Yahoo Chrome extension somewhere easy to find and open install UI."""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import repo_root

# Easy path for humans (not buried under .cursor/projects/…)
STAGED_DIR_NAME = "Yahoo Capture Extension"
EXTENSION_FILES = (
    "manifest.json",
    "background.js",
    "content.js",
    "bridge.js",
    "popup.html",
    "popup.js",
    "picker.html",
    "picker.js",
)


def extension_source() -> Path:
    return repo_root() / "extensions" / "browser-capture"


def staged_extension_dir() -> Path:
    return Path.home() / "AnalystLedger" / STAGED_DIR_NAME


def stage_yahoo_extension(*, dest: Optional[Path] = None) -> Path:
    """Copy extension files to ~/AnalystLedger/Yahoo Capture Extension."""
    src = extension_source()
    if not src.is_dir():
        raise FileNotFoundError(f"Extension source missing: {src}")
    missing = [name for name in EXTENSION_FILES if not (src / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Extension incomplete at {src}: missing {missing}")

    out = (dest or staged_extension_dir()).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    for name in EXTENSION_FILES:
        shutil.copy2(src / name, out / name)

    readme = out / "HOW TO INSTALL.txt"
    readme.write_text(
        "\n".join(
            [
                "Analyst Ledger / Flyleaf — Capture Chrome extension",
                "",
                "1. Open Chrome → chrome://extensions",
                "2. Turn on Developer mode (top right)",
                "3. Click Load unpacked",
                f"4. Choose this folder: {out}",
                "5. Sign in at https://levin.fly.dev (Capture posts there by default)",
                "6. On Tracking, click Start tracking — a tab picker opens to select tabs",
                "7. Browse research sites; visits show up under Tracking → Recent events",
                "",
                "If Capture shows as not connected: reload the extension, refresh Flyleaf.",
                "Chrome does not allow one-click install for private local extensions.",
                "Load unpacked once; after that it stays installed.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return out


def _open_paths(paths: List[str]) -> List[str]:
    """Best-effort open Finder / Chrome. Returns list of actions attempted."""
    system = platform.system()
    done: List[str] = []
    for path in paths:
        try:
            if system == "Darwin":
                subprocess.run(["open", path], check=False)
            elif system == "Windows":
                subprocess.run(["cmd", "/c", "start", "", path], check=False)
            else:
                subprocess.run(["xdg-open", path], check=False)
            done.append(path)
        except OSError:
            continue
    return done


def open_install_ui(staged: Path) -> Dict[str, Any]:
    """Reveal the staged folder and open Chrome extensions page."""
    opened: List[str] = []
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["open", "-R", str(staged)], check=False)
            opened.append(f"reveal:{staged}")
            # chrome:// URLs need AppleScript; plain `open` often ignores them
            chrome = Path("/Applications/Google Chrome.app")
            if chrome.exists():
                subprocess.run(
                    [
                        "osascript",
                        "-e",
                        'tell application "Google Chrome" to activate',
                        "-e",
                        'tell application "Google Chrome" to open location "chrome://extensions/"',
                    ],
                    check=False,
                )
                opened.append("chrome://extensions")
            else:
                opened.append("open Chrome manually → chrome://extensions")
        else:
            opened.extend(_open_paths([str(staged)]))
            opened.append("open Chrome → chrome://extensions")
    except OSError as exc:
        return {
            "staged": str(staged),
            "opened": opened,
            "warning": str(exc),
        }
    return {"staged": str(staged), "opened": opened}


def install_yahoo_extension(*, open_ui: bool = True) -> Dict[str, Any]:
    staged = stage_yahoo_extension()
    result: Dict[str, Any] = {
        "ok": True,
        "source": str(extension_source()),
        "staged": str(staged),
        "steps": [
            "In Chrome: turn on Developer mode",
            "Click Load unpacked",
            f"Select folder: {staged}",
        ],
    }
    if open_ui:
        result.update(open_install_ui(staged))
    return result
