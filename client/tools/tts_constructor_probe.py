"""Instrument CosyVoice3 constructor phases in-process, without patching assets."""

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
    import cosyvoice.cli.cosyvoice as cv

    emit("cosyvoice_imported")
    real_hyper = cv.load_hyperpyyaml
    real_frontend = cv.CosyVoiceFrontEnd
    real_model = cv.CosyVoice3Model

    def traced_hyper(*hyper_args, **hyper_kwargs):
        emit("hyper_start")
        value = real_hyper(*hyper_args, **hyper_kwargs)
        emit("hyper_done")
        return value

    cv.load_hyperpyyaml = traced_hyper

    class TracedModel(real_model):
        def __init__(self, *model_args, **model_kwargs):
            emit("model_construct_start")
            super().__init__(*model_args, **model_kwargs)
            emit("model_construct_done")

        def load(self, *load_args, **load_kwargs):
            emit("checkpoint_load_start", files=[Path(str(item)).name for item in load_args[:3]])
            value = super().load(*load_args, **load_kwargs)
            emit("checkpoint_load_done")
            return value

    class TracedFrontend(real_frontend):
        def __init__(self, *frontend_args, **frontend_kwargs):
            emit("frontend_start")
            super().__init__(*frontend_args, **frontend_kwargs)
            emit("frontend_done")
            # The model-type assertion runs immediately before this call;
            # replacing the global now preserves it and traces the next phase.
            cv.CosyVoice3Model = TracedModel

    cv.CosyVoiceFrontEnd = TracedFrontend
    emit("wrappers_ready")
    model = cv.AutoModel(model_dir=args.model_dir, fp16=args.fp16)
    emit("loaded", sample_rate=int(model.sample_rate))
    del model
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    emit("closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
