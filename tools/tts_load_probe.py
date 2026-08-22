"""Flush-at-each-stage CosyVoice3 local load probe for B06 diagnostics."""

from __future__ import annotations

import argparse
import os
import sys
import time
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--fp16", action="store_true")
    parser.add_argument("--progress-file", required=True)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    started = time.perf_counter()
    progress_path = Path(args.progress_file)
    progress_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(value: dict[str, object]) -> None:
        with progress_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")
            handle.flush()
        print(value, flush=True)

    emit({"stage": "start", "pid": os.getpid(), "python": sys.executable})
    runtime_root = Path(args.runtime_root)
    model_dir = Path(args.model_dir)
    sys.path.insert(0, str(runtime_root))
    emit({"stage": "paths", "runtime_exists": runtime_root.is_dir(), "model_exists": model_dir.is_dir()})
    import torch

    emit(
        {
            "stage": "torch",
            "version": torch.__version__,
            "cuda": bool(torch.cuda.is_available()),
            "device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        },
    )
    from cosyvoice.cli.cosyvoice import AutoModel

    emit({"stage": "imported", "elapsed_s": round(time.perf_counter() - started, 3)})
    model = AutoModel(model_dir=str(model_dir), fp16=args.fp16)
    emit(
        {
            "stage": "loaded",
            "sample_rate": int(model.sample_rate),
            "elapsed_s": round(time.perf_counter() - started, 3),
        },
    )
    del model
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    emit({"stage": "closed", "elapsed_s": round(time.perf_counter() - started, 3)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
