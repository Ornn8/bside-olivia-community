"""Isolated Breeze TTS 2 worker using the pinned community runtime."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import wave
from pathlib import Path
from typing import Any


_PACKAGE_NAME = "olivia_breeze_tts2_runtime"
_VARIANT_LABEL_ATTR = {
    "int8_hybrid": "HYBRID_LABEL",
    "bf16": "BF16_LABEL",
    "int8_convrot": "INT8_LABEL",
    "int8_text_encoder": "TE_INT8_LABEL",
}


def _write_status(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_package(runtime_root: Path):
    spec = importlib.util.spec_from_file_location(
        _PACKAGE_NAME,
        runtime_root / "__init__.py",
        submodule_search_locations=[str(runtime_root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("BREEZE_RUNTIME_INVALID")
    package = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE_NAME] = package
    spec.loader.exec_module(package)
    try:
        return (
            sys.modules[f"{_PACKAGE_NAME}.loader"],
            sys.modules[f"{_PACKAGE_NAME}.nodes"],
            sys.modules[f"{_PACKAGE_NAME}.runtime"],
        )
    except KeyError as exc:
        raise RuntimeError("BREEZE_RUNTIME_INVALID") from exc


def _read_reference_audio(path: Path) -> dict[str, Any]:
    import soundfile as sf
    import torch

    audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    mono = audio.mean(axis=1)
    if mono.size == 0 or int(sample_rate) <= 0:
        raise RuntimeError("BREEZE_REFERENCE_AUDIO_INVALID")
    return {
        "waveform": torch.from_numpy(mono).view(1, 1, -1),
        "sample_rate": int(sample_rate),
    }


def _write_wav(path: Path, waveform: Any, sample_rate: int, gain_db: float) -> None:
    import numpy

    values = waveform.detach().float().cpu().numpy().reshape(-1)
    gain = 10.0 ** (float(gain_db) / 20.0)
    pcm = numpy.rint(numpy.clip(values * gain, -1.0, 1.0) * 32767.0).astype("<i2")
    if pcm.size == 0 or int(sample_rate) <= 0:
        raise RuntimeError("BREEZE_EMPTY_AUDIO")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(int(sample_rate))
        target.writeframes(pcm.tobytes())


def _synthesize(request: dict[str, Any], output: Path, status: Path) -> None:
    ready = False
    try:
        _write_status(
            status,
            {"status": "initializing", "phase": "preflight", "audio_started": False},
        )
        runtime_root = Path(str(request["runtime_root"]))
        model_root = Path(str(request["model_dir"]))
        loader, nodes, runtime = _load_package(runtime_root)
        loader.model_dirs = lambda: [model_root]
        variant = str(request.get("model_variant", "int8_hybrid") or "int8_hybrid")
        try:
            label = getattr(loader, _VARIANT_LABEL_ATTR[variant])
        except (KeyError, AttributeError) as exc:
            raise RuntimeError("BREEZE_MODEL_VARIANT_UNSUPPORTED") from exc
        bundle = loader.load_breeze_bundle(
            label,
            str(request.get("dtype", "bf16") or "bf16"),
            str(request.get("device", "cuda") or "cuda"),
            str(request.get("attention", "eager") or "eager"),
            False,
            str(request.get("decode_mode", "eager") or "eager"),
        )
        reference = _read_reference_audio(Path(str(request["reference_audio"])))
        reference_waveform, reference_rate = runtime.comfy_audio_to_tensor(reference)
        reference_codes = runtime.encode_reference_audio(
            bundle.codec, reference_waveform, reference_rate
        )
        ready = True
        _write_status(
            status,
            {"status": "ready", "phase": "generation", "audio_started": False},
        )
        result = nodes._generate_audio(
            bundle,
            text=str(request["text"]),
            instruction=str(request["instruction"]),
            ref_audio=None,
            ref_text=str(request["reference_text"]),
            cfg_scale=float(request.get("cfg_scale", 4.0)),
            max_new_tokens=int(request.get("max_new_tokens", 1500)),
            temperature=float(request.get("temperature", 0.9)),
            top_k=int(request.get("top_k", 50)),
            top_p=float(request.get("top_p", 1.0)),
            repetition_penalty=float(request.get("repetition_penalty", 1.1)),
            depth_temperature=float(request.get("depth_temperature", 0.9)),
            depth_top_k=int(request.get("depth_top_k", 50)),
            depth_top_p=float(request.get("depth_top_p", 1.0)),
            seed=int(request.get("seed", 200717)),
            ref_codes=reference_codes,
            progress_callback=None,
            progress_label=None,
        )
        _write_wav(
            output,
            result["waveform"],
            int(result["sample_rate"]),
            float(request.get("gain_db", 0.0)),
        )
        _write_status(
            status,
            {"status": "completed", "phase": "completed", "audio_started": True},
        )
    except Exception:
        _write_status(
            status,
            {
                "status": "failed",
                "phase": "generation" if ready else "preflight",
                "audio_started": ready,
            },
        )
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    args = parser.parse_args()
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        _synthesize(request, args.output, args.status)
    except Exception:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
