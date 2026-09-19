"""Convenience entry point for the B10B lifecycle CLI."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.packaging.b10b.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
