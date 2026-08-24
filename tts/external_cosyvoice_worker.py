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


def _append_with_cross_fade(
    target: list[float],
    block: list[float],
    overlap: int,
) -> None:
    if not target or overlap <= 0:
        target.extend(block)
        return
    count = min(overlap, len(target), len(block))
    start = len(target) - count
    for index in range(count):
        weight = (index + 1) / (count + 1)
        target[start + index] = (
            target[start + index] * (1.0 - weight) + block[index] * weight
        )
    target.extend(block[count:])


def _synthesize(request: dict[str, object], output: Path) -> None:
    runtime_root = Path(str(request["runtime_root"]))
    sys.path.insert(0, str(runtime_root))
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "MODELSCOPE_OFFLINE"):
        os.environ[key] = "1"
    import torch
    from cosyvoice.cli.cosyvoice import AutoModel

    torch.manual_seed(int(request.get("seed", 200717)))
    model = AutoModel(model_dir=str(request["model_dir"]), fp16=bool(request.get("fp16", True)))
    prompt_prefix = "You are a helpful assistant.<|endofprompt|>"
    raw_blocks = request.get("blocks")
    if isinstance(raw_blocks, list) and raw_blocks:
        blocks = [str(value).strip() for value in raw_blocks if str(value).strip()]
    else:
        text = str(request.get("text", "")).strip()
        blocks = [text] if text else []
    if not blocks:
        raise RuntimeError("empty CosyVoice input")
    samples: list[float] = []
    cross_fade = max(0.0, min(0.5, float(request.get("cross_fade_seconds", 0.0))))
    overlap = round(cross_fade * int(model.sample_rate))
    for block in blocks:
        block_samples: list[float] = []
        for item in model.inference_cross_lingual(
            prompt_prefix + block,
            str(request["reference_audio"]),
            zero_shot_spk_id="",
            stream=False,
            speed=1.0,
        ):
            speech = item.get("tts_speech") if isinstance(item, dict) else None
            if speech is not None:
                block_samples.extend(
                    float(value)
                    for value in speech.detach().cpu().float().reshape(-1).tolist()
                )
        if not block_samples:
            raise RuntimeError("empty CosyVoice block output")
        _append_with_cross_fade(samples, block_samples, overlap)
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
