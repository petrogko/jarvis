"""Entry point: `python -m jarvis_sidecar`."""

from __future__ import annotations

import sys


def main() -> int:
    # Wired in T2 once the FastAPI app exists.
    print("jarvis-sidecar: entry-point stub. T2 wires the FastAPI app.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
