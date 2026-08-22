"""One-shot external-venv adapter for the maintained CosyVoice runtime.

This file deliberately has no project-package imports.  It is run by the
already-installed CosyVoice venv and uses only the pinned runtime/model/audio
references supplied in a private temporary request file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import wave
from array import array
from pathlib import Path


def _write_wav(path: Path, sample_rate: int, samples: list[float]) -> None:
    pcm = array("h", (max(-32768, min(32767, round(value * 32767.0))) for value in samples))
    if sys.byteorder != "little":
        pcm.byteswap()
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(pcm.tobytes())


def _synthesize(request: dict[str, object], output: Path) -> None:
    runtime_root = Path(str(request["runtime_root"]))
    sys.path.insert(0, str(runtime_root))
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "MODELSCOPE_OFFLINE"):
        os.environ[key] = "1"
    from cosyvoice.cli.cosyvoice import AutoModel

    model = AutoModel(model_dir=str(request["model_dir"]), fp16=bool(request.get("fp16", True)))
    prompt = "You are a helpful assistant.<|endofprompt|>" + str(request["reference_text"])
    samples: list[float] = []
    for item in model.inference_zero_shot(
        str(request["text"]),
        prompt,
        str(request["reference_audio"]),
        stream=bool(request.get("stream", True)),
        speed=float(request.get("speed", 1.0)),
    ):
        speech = item.get("tts_speech") if isinstance(item, dict) else None
        if speech is not None:
            samples.extend(float(value) for value in speech.detach().cpu().float().reshape(-1).tolist())
    if not samples:
        raise RuntimeError("empty CosyVoice output")
    _write_wav(output, int(model.sample_rate), samples)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    _synthesize(request, Path(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
