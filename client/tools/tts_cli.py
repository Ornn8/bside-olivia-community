"""B06 local TTS profile CLI.

Examples (all model/audio paths are external and are never copied):

    rtk python tools/tts_cli.py install --state-root .evidence/tts-state ...
    rtk python tools/tts_cli.py doctor --state-root .evidence/tts-state --profile cosyvoice3-live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tts import TTSConfig, TTSProfileManager, TTSRequest, TTSService  # noqa: E402
from tts.contracts import TTSError  # noqa: E402


def _json_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _manager(args: argparse.Namespace) -> TTSProfileManager:
    if not args.state_root:
        raise TTSError("TTS_STATE_ROOT_REQUIRED")
    return TTSProfileManager(args.state_root)


def _install_config(args: argparse.Namespace) -> TTSConfig:
    provider_options: dict[str, Any] = {
        "prompt_prefix": args.prompt_prefix,
        "text_frontend": args.text_frontend,
    }
    if args.numba_cache_dir:
        provider_options["numba_cache_dir"] = args.numba_cache_dir
    if args.temp_root:
        provider_options["temp_root"] = args.temp_root
    if args.wetext_fst_root:
        provider_options["wetext_fst_root"] = args.wetext_fst_root
    return TTSConfig(
        profile=args.profile,
        provider=args.provider,
        enabled=not args.disabled,
        runtime_root=args.runtime_root,
        model_dir=args.model_dir,
        reference_audio=args.reference_audio,
        reference_text=args.reference_text,
        language=args.language,
        license_id=args.license_id,
        fallback=args.fallback,
        speed=args.speed,
        leading_trim_seconds=args.leading_trim_seconds,
        max_input_chars=args.max_input_chars,
        fp16=not args.no_fp16,
        provider_options=provider_options,
    )


async def _synthesize(manager: TTSProfileManager, args: argparse.Namespace) -> dict[str, Any]:
    config = manager.config(args.profile)
    service = TTSService(config)
    try:
        result = await service.synthesize(
            TTSRequest(args.text, stream=not args.no_stream),
            output_path=args.output,
        )
        return asdict(result)
    finally:
        service.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="B06 standalone local TTS profile manager")
    parser.add_argument("--state-root", required=True, help="local metadata/evidence root")
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install")
    install.add_argument("--profile", default="cosyvoice3-live")
    install.add_argument("--provider", default="cosyvoice3")
    install.add_argument("--runtime-root", required=True)
    install.add_argument("--model-dir", required=True)
    install.add_argument("--reference-audio", required=True)
    install.add_argument("--reference-text", required=True)
    install.add_argument("--language", default="zh")
    install.add_argument("--license-id", default="Apache-2.0")
    install.add_argument("--fallback", choices=("text", "unavailable"), default="text")
    install.add_argument("--speed", type=float, default=1.0)
    install.add_argument("--leading-trim-seconds", type=float, default=0.0)
    install.add_argument("--max-input-chars", type=int, default=12000)
    install.add_argument("--prompt-prefix", default="You are a helpful assistant.<|endofprompt|>")
    install.add_argument("--text-frontend", choices=("none", "local"), default="none")
    install.add_argument("--wetext-fst-root", default="")
    install.add_argument("--numba-cache-dir", default="")
    install.add_argument("--temp-root", default="")
    install.add_argument("--no-fp16", action="store_true")
    install.add_argument("--disabled", action="store_true")
    install.add_argument("--dry-run", action="store_true")

    for name in ("doctor", "disable", "enable", "uninstall"):
        command = sub.add_parser(name)
        command.add_argument("--profile", default="cosyvoice3-live")
    sub.choices["uninstall"].add_argument("--apply", action="store_true")

    customize = sub.add_parser("customize")
    customize.add_argument("--profile", default="cosyvoice3-live")
    customize.add_argument("--set", dest="changes", action="append", default=[], metavar="KEY=VALUE")

    list_command = sub.add_parser("list")
    list_command.set_defaults(command="list")

    synthesize = sub.add_parser("synthesize")
    synthesize.add_argument("--profile", default="cosyvoice3-live")
    synthesize.add_argument("--text", required=True)
    synthesize.add_argument("--output", required=True)
    synthesize.add_argument("--no-stream", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        manager = _manager(args)
        if args.command == "install":
            result = manager.install(_install_config(args), dry_run=args.dry_run)
        elif args.command == "doctor":
            result = manager.doctor(args.profile)
        elif args.command == "disable":
            result = manager.set_enabled(args.profile, False)
        elif args.command == "enable":
            result = manager.set_enabled(args.profile, True)
        elif args.command == "uninstall":
            result = manager.uninstall(args.profile, dry_run=not args.apply)
        elif args.command == "customize":
            changes: dict[str, Any] = {}
            for item in args.changes:
                if "=" not in item:
                    raise TTSError("TTS_CUSTOMIZE_FORMAT")
                key, value = item.split("=", 1)
                changes[key.strip()] = _json_value(value)
            result = manager.customize(args.profile, changes)
        elif args.command == "list":
            result = {"status": "OK", "profiles": manager.list_profiles()}
        elif args.command == "synthesize":
            result = asyncio.run(_synthesize(manager, args))
        else:  # pragma: no cover
            raise TTSError("TTS_COMMAND_UNKNOWN")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (TTSError, OSError, ValueError) as exc:
        print(json.dumps({"status": "ERROR", "code": getattr(exc, "code", "TTS_CLI_ERROR")}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
