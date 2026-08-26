"""One-shot external-venv adapter for the maintained CosyVoice runtime.

This file deliberately has no project-package imports.  It is run by the
already-installed CosyVoice venv and uses only the pinned runtime/model/audio
references supplied in a private temporary request file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import wave
from array import array
from pathlib import Path


_END_OF_PROMPT_TOKEN = "<|endofprompt|>"
_COSYVOICE_BASE_LLM_SHA256 = "69f43bd545131c30e98947fb360ea8b4dc9916d8e83dded7757c7ea4f5a24970"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_base_model(model_dir: Path) -> None:
    checkpoint = model_dir / "llm.pt"
    if not checkpoint.is_file() or _sha256(checkpoint) != _COSYVOICE_BASE_LLM_SHA256:
        raise RuntimeError("COSYVOICE_BASE_MODEL_HASH_MISMATCH")


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
    if request.get("voice_condition_mode") == "instruct2_single_pass":
        if str(request.get("llm_variant", "base")) != "base":
            raise RuntimeError("only the accepted base llm.pt variant is supported")
        _validate_base_model(Path(str(request["model_dir"])))
    sys.path.insert(0, str(runtime_root))
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "MODELSCOPE_OFFLINE"):
        os.environ[key] = "1"
    from cosyvoice.cli.cosyvoice import AutoModel

    model = AutoModel(model_dir=str(request["model_dir"]), fp16=bool(request.get("fp16", True)))
    if request.get("voice_condition_mode") == "instruct2_single_pass":
        _synthesize_instruct2_single_pass(model, request, output)
        return
    if request.get("voice_condition_mode") == "contextual_long_form" or request.get("blocks"):
        _synthesize_delivery(model, request, output)
        return
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


def _synthesize_instruct2_single_pass(model, request: dict[str, object], output: Path) -> None:
    """Apply one LLM-chosen global style without placing it in spoken text."""

    if str(request.get("llm_variant", "base")) != "base":
        raise RuntimeError("only the accepted base llm.pt variant is supported")
    if not callable(getattr(model, "inference_instruct2", None)):
        raise RuntimeError("COSYVOICE_INSTRUCT2_UNSUPPORTED")
    text = str(request.get("text", ""))
    instruction = str(request.get("instruct_text", "")).strip()
    if not text.strip() or not instruction:
        raise RuntimeError("single-pass text or instruction missing")
    if _END_OF_PROMPT_TOKEN in text:
        raise RuntimeError("TTS_DIRECTED_TEXT_CONTAINS_CONTROL_TOKEN")
    instruction = instruction.replace(_END_OF_PROMPT_TOKEN, "").rstrip()
    instruction += _END_OF_PROMPT_TOKEN
    speed = max(0.96, min(1.15, float(request.get("speed", 1.0))))
    gain_db = max(-0.75, min(0.75, float(request.get("gain_db", 0.0))))
    gain = 10.0 ** (gain_db / 20.0)
    raw_target = request.get("duration_target_seconds", [40.0, 50.0])
    if not isinstance(raw_target, list) or len(raw_target) != 2:
        raw_target = [40.0, 50.0]
    target_min = max(1.0, float(raw_target[0]))
    target_max = max(target_min, float(raw_target[1]))
    max_attempts = max(1, min(3, int(request.get("max_attempts", 1))))
    base_seed = int(request.get("seed", 200717))
    best_samples: list[float] = []
    best_distance = float("inf")

    for attempt in range(max_attempts):
        seed = base_seed + attempt
        random.seed(seed)
        try:
            import numpy as np

            np.random.seed(seed % (2**32))
        except ImportError:
            pass
        try:
            import torch

            torch.manual_seed(seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(seed)
        except ImportError:
            pass

        samples: list[float] = []
        for item in model.inference_instruct2(
            text,
            instruction,
            str(request["reference_audio"]),
            zero_shot_spk_id="",
            stream=False,
            speed=speed,
            text_frontend=False,
        ):
            speech = item.get("tts_speech") if isinstance(item, dict) else None
            if speech is not None:
                samples.extend(
                    float(value) * gain
                    for value in speech.detach().cpu().float().reshape(-1).tolist()
                )
        if not samples:
            continue
        duration = len(samples) / int(model.sample_rate)
        distance = max(target_min - duration, 0.0, duration - target_max)
        if distance < best_distance:
            best_samples = samples
            best_distance = distance
        if target_min <= duration <= target_max:
            _write_wav(output, int(model.sample_rate), samples)
            return
    if not best_samples:
        raise RuntimeError("empty CosyVoice single-pass output")
    _write_wav(output, int(model.sample_rate), best_samples)


def _cross_fade(chunks: list[list[float]], sample_rate: int, seconds: float) -> list[float]:
    if not chunks:
        return []
    output = list(chunks[0])
    requested = max(0, round(sample_rate * seconds))
    for chunk in chunks[1:]:
        overlap = min(requested, len(output), len(chunk))
        if overlap <= 0:
            output.extend(chunk)
            continue
        start = len(output) - overlap
        denominator = max(1, overlap - 1)
        for index in range(overlap):
            weight = index / denominator
            output[start + index] = output[start + index] * (1.0 - weight) + chunk[index] * weight
        output.extend(chunk[overlap:])
    return output


def _synthesize_delivery(model, request: dict[str, object], output: Path) -> None:
    """Use the maintained frontend for coherent blocks, then hide its joins."""

    try:
        import torch

        torch.manual_seed(int(request.get("seed", 200717)))
    except ImportError:
        pass

    raw_blocks = request.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise RuntimeError("delivery blocks missing")
    blocks = [str(block) for block in raw_blocks]
    if any(not block.strip() for block in blocks):
        raise RuntimeError("delivery block missing")
    reference_audio = str(request["reference_audio"])
    default_speed = max(0.96, min(1.15, float(request.get("speed", 1.0))))
    raw_controls = request.get("block_controls")
    controls = raw_controls if isinstance(raw_controls, list) else []
    if controls and len(controls) != len(blocks):
        raise RuntimeError("delivery block controls mismatch")
    chunks: list[list[float]] = []
    pauses: list[float] = []
    for index, block in enumerate(blocks):
        control = controls[index] if controls and isinstance(controls[index], dict) else {}
        speed = max(0.96, min(1.15, float(control.get("speed", default_speed))))
        pause = max(0.0, min(0.65, float(control.get("pause_after_seconds", 0.0))))
        gain_db = max(-1.5, min(1.5, float(control.get("gain_db", 0.0))))
        gain = 10.0 ** (gain_db / 20.0)
        model_text = "You are a helpful assistant.<|endofprompt|>" + block
        block_samples: list[float] = []
        for item in model.inference_cross_lingual(
            model_text,
            reference_audio,
            zero_shot_spk_id="",
            stream=False,
            speed=speed,
        ):
            speech = item.get("tts_speech") if isinstance(item, dict) else None
            if speech is not None:
                block_samples.extend(
                    float(value) * gain
                    for value in speech.detach().cpu().float().reshape(-1).tolist()
                )
        if not block_samples:
            raise RuntimeError("empty CosyVoice delivery block")
        chunks.append(block_samples)
        pauses.append(pause)

    sample_rate = int(model.sample_rate)
    cross_fade_seconds = max(
        0.0, min(0.25, float(request.get("cross_fade_seconds", 0.15)))
    )
    samples: list[float] = []
    for index, chunk in enumerate(chunks):
        if not samples:
            samples.extend(chunk)
        elif pauses[index - 1] > 0:
            samples.extend([0.0] * round(sample_rate * pauses[index - 1]))
            samples.extend(chunk)
        else:
            samples = _cross_fade([samples, chunk], sample_rate, cross_fade_seconds)
    if pauses and pauses[-1] > 0:
        samples.extend([0.0] * round(sample_rate * pauses[-1]))
    if not samples:
        raise RuntimeError("empty CosyVoice delivery output")
    _write_wav(output, sample_rate, samples)


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
