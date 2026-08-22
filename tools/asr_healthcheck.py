"""Direct ASR provider health diagnostic.

The default mode is offline and may report a valid ``UNAVAILABLE`` result.
``--probe`` performs the real local ``/ready`` HTTP request; ``--require-ready``
turns an unavailable result into a non-zero exit for acceptance automation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.config import AsrConfig  # noqa: E402
from asr.provider import NemotronProvider, create_provider  # noqa: E402


async def _run(config: AsrConfig, *, probe: bool) -> dict[str, object]:
    provider = create_provider(config)
    if probe and isinstance(provider, NemotronProvider):
        status = await provider.probe_ready()
    else:
        status = provider.status()
    return {
        "config": config.to_dict(include_paths=False),
        "status": status,
        "probe_requested": probe,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose the local B05 ASR provider")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args(argv)
    config = AsrConfig.from_json(args.config) if args.config else AsrConfig.from_env()
    result = asyncio.run(_run(config, probe=args.probe))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if not args.require_ready or result["status"].get("status") == "available" else 1


if __name__ == "__main__":
    raise SystemExit(main())
