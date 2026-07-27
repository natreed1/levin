"""Run: python -m messenger"""

from __future__ import annotations

import os
import sys

import uvicorn


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
    host = os.environ.get("MESSENGER_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT") or os.environ.get("MESSENGER_PORT") or "8790")
    _enable_local_dev_defaults(host)
    uvicorn.run("messenger.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
