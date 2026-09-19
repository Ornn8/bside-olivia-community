"""Explicit local launcher for the B10B-to-B08 bridge.

It reads only the process ``DEEPSEEK_API_KEY`` when present.  The key is never
written to B10B metadata, reports, or command arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.packaging.b10b.errors import B10BError
from runtime.packaging.b10b.live_bridge import build_live_service_from_b10b
from runtime.packaging.b10b.security import redact
from tools.live_healthcheck import run_voice_turn


def launcher_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Create the explicit DeepSeek Flash environment without accepting key aliases."""

    values = source if source is not None else os.environ
    result = {
        "OLIVIA_LLM_PROVIDER": "openai_compatible",
        "OLIVIA_LLM_BASE_URL": "https://api.deepseek.com",
        "OLIVIA_LLM_MODEL": "deepseek-v4-flash",
        "OLIVIA_LLM_API_KEY_ENV": "DEEPSEEK_API_KEY",
        "OLIVIA_LLM_REQUIRES_API_KEY": "true",
        "OLIVIA_LLM_STREAM": "true",
    }
    if "DEEPSEEK_API_KEY" in values:
        result["DEEPSEEK_API_KEY"] = str(values["DEEPSEEK_API_KEY"])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one B10B-assembled B08 turn with DeepSeek V4 Flash configuration.")
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--text", required=True, help="one local text turn")
    parser.add_argument("--output-wav", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        service = build_live_service_from_b10b(
            project_root=args.project_root,
            data_root=args.data_root,
            environ=launcher_environment(),
        )
        report = asyncio.run(run_voice_turn(service=service, text=args.text, output_path=args.output_wav))
        rendered = json.dumps(redact(report), ensure_ascii=False, sort_keys=True)
        if args.report is not None:
            args.report.absolute().parent.mkdir(parents=True, exist_ok=True)
            args.report.absolute().write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0
    except B10BError as exc:
        print(json.dumps({"status": "ERROR", "code": exc.code, "details": redact(exc.details)}, ensure_ascii=False, sort_keys=True))
        return exc.exit_code
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "FAILED", "error_code": type(exc).__name__}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
