"""Trace the expensive local CosyVoice3 constructor without touching assets."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--progress-file", required=True)
    parser.add_argument("--fp16", action="store_true")
    args = parser.parse_args()
    progress = Path(args.progress_file)
    progress.parent.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()

    def emit(stage: str, **values: object) -> None:
        record = {"stage": stage, "elapsed_s": round(time.perf_counter() - start, 3), **values}
        with progress.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
        print(record, flush=True)

    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    emit("start", pid=os.getpid(), python=sys.executable)
    sys.path.insert(0, args.runtime_root)
    import torch

    emit("torch_imported", version=torch.__version__, cuda=bool(torch.cuda.is_available()))
    real_load = torch.load

    def traced_load(*load_args, **load_kwargs):
        path = str(load_args[0]) if load_args else "<missing>"
        emit("torch_load_start", path=Path(path).name, bytes=Path(path).stat().st_size if Path(path).is_file() else None)
        result = real_load(*load_args, **load_kwargs)
        emit("torch_load_done", path=Path(path).name)
        return result

    torch.load = traced_load
    emit("torch_load_traced")
    import onnxruntime

    real_session = onnxruntime.InferenceSession

    def traced_session(*session_args, **session_kwargs):
        path = str(session_args[0]) if session_args else "<missing>"
        emit("onnx_session_start", path=Path(path).name)
        result = real_session(*session_args, **session_kwargs)
        emit("onnx_session_done", path=Path(path).name)
        return result

    onnxruntime.InferenceSession = traced_session
    emit("onnx_traced")
    from cosyvoice.cli.cosyvoice import AutoModel

    emit("provider_imported")
    model = AutoModel(model_dir=args.model_dir, fp16=args.fp16)
    emit("loaded", sample_rate=int(model.sample_rate))
    del model
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    emit("closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
