"""Run: python -m messenger"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn


def _load_dotenv_files() -> None:
    """Load repo / messenger .env into os.environ (does not override existing)."""
    here = Path(__file__).resolve().parent
    candidates = [
        here / ".env",
        here.parent / ".env",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key or key in os.environ:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            os.environ[key] = value


def _enable_local_dev_defaults(host: str) -> None:
    """Convenience for `python -m messenger` on loopback — never on Fly."""
    if (os.environ.get("FLY_APP_NAME") or "").strip():
        return
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    if not loopback:
        return
    # Opt out with MESSENGER_DEV_AUTO_LOGIN=0
    if "MESSENGER_DEV_AUTO_LOGIN" not in os.environ:
        os.environ["MESSENGER_DEV_AUTO_LOGIN"] = "1"
    if os.environ.get("MESSENGER_DEV_AUTO_LOGIN", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        email = (os.environ.get("MESSENGER_DEV_EMAIL") or "dev@example.com").strip()
        name = (os.environ.get("MESSENGER_DEV_NAME") or "Dev").strip()
        print(
            f"Dev auto-login on → {name} <{email}> "
            "(set MESSENGER_DEV_AUTO_LOGIN=0 to require a password)",
            file=sys.stderr,
        )


def main() -> None:
    _load_dotenv_files()
    host = os.environ.get("MESSENGER_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT") or os.environ.get("MESSENGER_PORT") or "8790")
    _enable_local_dev_defaults(host)
    uvicorn.run("messenger.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
