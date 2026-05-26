"""Entry point: `python -m jarvis_sidecar`."""

from __future__ import annotations

import sys

import uvicorn

from . import config


def main() -> int:
    uvicorn.run(
        "jarvis_sidecar.app:app",
        host=config.BIND_HOST,
        port=config.BIND_PORT,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
