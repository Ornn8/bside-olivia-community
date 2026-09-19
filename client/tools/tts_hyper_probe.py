"""Identify which HyperPyYAML constructor consumes the CosyVoice3 load time."""

from __future__ import annotations

import argparse
import faulthandler
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
    parser.add_argument("--stack-file", required=True)
    args = parser.parse_args()
    progress = Path(args.progress_file)
    progress.parent.mkdir(parents=True, exist_ok=True)
    stack_file = Path(args.stack_file)
    stack_handle = stack_file.open("w", encoding="utf-8", newline="\n")
    faulthandler.dump_traceback_later(15, repeat=True, file=stack_handle)
    started = time.perf_counter()

    def emit(stage: str, **values: object) -> None:
        record = {"stage": stage, "elapsed_s": round(time.perf_counter() - started, 3), **values}
        with progress.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
        print(record, flush=True)

    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    emit("start", pid=os.getpid(), python=sys.executable)
    sys.path.insert(0, args.runtime_root)
    import cosyvoice.llm.llm as llm_module
    import cosyvoice.flow.flow as flow_module
    import cosyvoice.flow.flow_matching as matching_module
    import cosyvoice.flow.DiT.dit as dit_module
    import cosyvoice.hifigan.generator as hift_module
    import cosyvoice.hifigan.hifigan as hifigan_module
    import cosyvoice.hifigan.discriminator as discriminator_module
    import matcha.hifigan.models as matcha_hifigan_module
    from hyperpyyaml import load_hyperpyyaml

    def traced_class(module, name: str):
        original = getattr(module, name)

        class Traced(original):
            def __init__(self, *class_args, **class_kwargs):
                emit(f"{name}_start")
                super().__init__(*class_args, **class_kwargs)
                emit(f"{name}_done")

        Traced.__name__ = name
        setattr(module, name, Traced)

    traced_class(llm_module, "Qwen2Encoder")
    traced_class(llm_module, "CosyVoice3LM")
    traced_class(flow_module, "CausalMaskedDiffWithDiT")
    traced_class(matching_module, "CausalConditionalCFM")
    traced_class(dit_module, "DiT")
    traced_class(hift_module, "CausalHiFTGenerator")
    traced_class(hifigan_module, "HiFiGan")
    traced_class(discriminator_module, "MultiResSpecDiscriminator")
    traced_class(matcha_hifigan_module, "MultiPeriodDiscriminator")
    emit("wrappers_ready")
    yaml_path = Path(args.model_dir) / "cosyvoice3.yaml"
    with yaml_path.open("r", encoding="utf-8") as handle:
        configs = load_hyperpyyaml(
            handle,
            overrides={"qwen_pretrain_path": str(Path(args.model_dir) / "CosyVoice-BlankEN")},
        )
    emit("hyper_done", keys=sorted(str(key) for key in configs.keys()))
    faulthandler.cancel_dump_traceback_later()
    stack_handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
