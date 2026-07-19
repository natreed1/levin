"""Run: python -m messenger"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("MESSENGER_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT") or os.environ.get("MESSENGER_PORT") or "8790")
    uvicorn.run("messenger.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
