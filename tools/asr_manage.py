"""Command-line install, switch, status, and uninstall for B05."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.config import AsrConfig  # noqa: E402
from asr.cuda_toolchain import (  # noqa: E402
    assemble_cuda_toolchain,
    build_environment,
    cuda_toolchain_status,
    inspect_cuda_transfer,
    uninstall_cuda_toolchain,
)
from asr.management import install, switch_provider, uninstall  # noqa: E402
from asr.provider import NemotronProvider, create_provider  # noqa: E402


DEFAULT_CONFIG = Path("F:/Bside-olivia-local/asr/config.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the optional local B05 ASR runtime")
    parser.add_argument("command", choices=["status", "install", "uninstall", "switch", "cuda-toolchain"])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--provider", choices=["text-fallback", "nemotron-speech-cpp"])
    parser.add_argument(
        "--data-root",
        type=Path,
        help="explicit D:/ or F:/ root for runtime, model, cache, and acceptance evidence",
    )
    parser.add_argument("--transfer-root", type=Path, help="verified offline D:/ or F:/ transfer root")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--action", choices=["status", "assemble", "uninstall"], default="status")
    parser.add_argument("--cuda-root", type=Path)
    parser.add_argument("--cuda-manifest", type=Path)
    parser.add_argument("--cuda-transfer-root", type=Path)
    parser.add_argument("--cmake", type=Path)
    parser.add_argument("--ninja", type=Path)
    parser.add_argument("--vswhere", type=Path)
    parser.add_argument("--cuda-arch", default="86")
    args = parser.parse_args(argv)

    if args.command == "cuda-toolchain":
        if not args.cuda_root:
            parser.error("cuda-toolchain requires --cuda-root")
        if args.action == "status":
            result = {"toolchain": cuda_toolchain_status(args.cuda_root)}
            result["environment"] = build_environment(
                args.cuda_root,
                cmake_path=args.cmake,
                ninja_path=args.ninja,
                vswhere_path=args.vswhere,
                cuda_arch=args.cuda_arch,
            )
            if args.cuda_manifest and args.cuda_transfer_root:
                result["transfer"] = inspect_cuda_transfer(args.cuda_manifest, args.cuda_transfer_root)
        elif args.action == "assemble":
            if not args.cuda_manifest or not args.cuda_transfer_root:
                parser.error("cuda-toolchain assemble requires --cuda-manifest and --cuda-transfer-root")
            result = assemble_cuda_toolchain(
                args.cuda_manifest,
                args.cuda_transfer_root,
                args.cuda_root,
                apply=args.apply,
            )
        else:
            result = uninstall_cuda_toolchain(args.cuda_root, apply=args.apply)
    elif args.command == "switch":
        if not args.provider:
            parser.error("switch requires --provider")
        seed = AsrConfig.from_json(args.config) if args.config.is_file() else AsrConfig.from_env()
        if args.data_root:
            data_root = args.data_root.absolute()
            seed = replace(
                seed,
                runtime_root=data_root / "runtime",
                model_root=data_root / "models",
                cache_root=data_root / "cache",
                model_path=data_root / "models" / "nemotron-3.5-asr-streaming-0.6b.q8_0.gguf",
            )
            seed.validate_storage_roots()
        result = switch_provider(args.config, args.provider, base_config=seed).to_dict()
    else:
        config = AsrConfig.from_json(args.config) if args.config.is_file() else AsrConfig.from_env()
        if args.command == "status":
            provider = create_provider(config)
            result = {"config": config.to_dict(include_paths=False), "status": provider.status()}
        elif args.command == "install":
            result = install(config, apply=args.apply, transfer_root=args.transfer_root)
        else:
            result = uninstall(config, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
