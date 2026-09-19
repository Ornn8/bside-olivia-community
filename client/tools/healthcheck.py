"""Offline healthcheck for core and optional profile metadata."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check local contract and optional profile metadata.")
    parser.add_argument("--profile", default="core", choices=["core", "llm", "memory", "asr"])
    args = parser.parse_args(argv)

    # Import-time diagnostics belong on stderr so stdout remains one JSON result.
    with contextlib.redirect_stdout(sys.stderr):
        import local_server  # noqa: E402

    result = asyncio.run(local_server.route("GET", "/health", {}, {"profile": args.profile}))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    valid_statuses = {"HEALTHY", "DEGRADED", "UNAVAILABLE"}
    return 0 if result.get("code") == 0 and result.get("data", {}).get("status") in valid_statuses else 1


if __name__ == "__main__":
    raise SystemExit(main())
