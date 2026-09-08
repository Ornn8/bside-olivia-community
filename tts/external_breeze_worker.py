"""Isolated Breeze TTS 2 worker using the pinned community runtime."""

from __future__ import annotations

import argparse
import gc
import importlib.util
import json
import os
import sys
import time
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
_WORKER_PHASES = frozenset({"request", "preflight", "package_load", "model_load",
    "reference_read", "reference_encode", "generation", "decoding", "audio_write", "completed"})
_WORKER_ERRORS = frozenset({"BREEZE_RUNTIME_INVALID", "BREEZE_REFERENCE_AUDIO_INVALID",
    "BREEZE_ADAPTER_INVALID",
    "BREEZE_EMPTY_AUDIO", "BREEZE_MODEL_VARIANT_UNSUPPORTED", "BREEZE_CUDA_OUT_OF_MEMORY",
    "BREEZE_CUDA_RUNTIME_FAILED", "BREEZE_MODULE_MISSING", "BREEZE_IMPORT_FAILED",
    "BREEZE_FILE_MISSING", "BREEZE_PERMISSION_DENIED", "BREEZE_DISK_FULL",
    "BREEZE_IO_FAILED", "BREEZE_REQUEST_INVALID", "BREEZE_RUNTIME_FAILED"})
_WORKER_EXCEPTION_TYPES = frozenset({"Exception", "RuntimeError", "ValueError", "TypeError",
    "KeyError", "AttributeError", "OSError", "FileNotFoundError", "PermissionError",
    "ImportError", "ModuleNotFoundError", "MemoryError", "OutOfMemoryError", "JSONDecodeError"})


def project_worker_status(value: object) -> dict[str, str]:
    """Strict shared projection for the local log and exported support bundle."""
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key, allowed in (
        ("phase", _WORKER_PHASES), ("error_code", _WORKER_ERRORS),
        ("error_type", _WORKER_EXCEPTION_TYPES),
    ) if isinstance(value.get(key), str) and value[key] in allowed}


def _worker_error_code(error: Exception) -> str:
    # Inspect locally; only fixed categories ever cross the process boundary.
    message = str(error)
    if message in _WORKER_ERRORS:
        return message
    lowered = message.casefold()
    if "cuda" in lowered and "out of memory" in lowered:
        return "BREEZE_CUDA_OUT_OF_MEMORY"
    if "cuda" in lowered:
        return "BREEZE_CUDA_RUNTIME_FAILED"
    if isinstance(error, ModuleNotFoundError):
        return "BREEZE_MODULE_MISSING"
    if isinstance(error, ImportError) or "dll load failed" in lowered:
        return "BREEZE_IMPORT_FAILED"
    if isinstance(error, FileNotFoundError):
        return "BREEZE_FILE_MISSING"
    if isinstance(error, PermissionError):
        return "BREEZE_PERMISSION_DENIED"
    if isinstance(error, OSError):
        return "BREEZE_DISK_FULL" if error.errno == 28 or getattr(error, "winerror", None) == 112 else "BREEZE_IO_FAILED"
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return "BREEZE_REQUEST_INVALID"
    return "BREEZE_RUNTIME_FAILED"


def _write_failure(status: Path, phase: str, error: Exception) -> None:
    kind = type(error).__name__
    try:
        _write_status(status, {"status": "failed", **project_worker_status({
            "phase": phase, "error_code": _worker_error_code(error),
            "error_type": kind if kind in _WORKER_EXCEPTION_TYPES else "Exception",
        })})
    except OSError:
        pass  # Preserve the original process failure when telemetry cannot be written.


def _write_status(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        os.replace(temporary, path)
    except PermissionError:
        # Windows readers can briefly prevent replacement. Telemetry must not
        # abort synthesis; the next progress event publishes fresh status.
        temporary.unlink(missing_ok=True)


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
    decode = None
    register = None
    adapter_receipt = {}
    phase = "preflight"
    started = time.monotonic()

    def progress(current: int, total: int) -> None:
        _write_status(status, {
            "status": "ready", "phase": "generation", "audio_started": False,
            "generated_frames": current, "max_frames": total,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        })

    try:
        _write_status(
            status,
            {"status": "initializing", "phase": "preflight", "audio_started": False},
        )
        runtime_root = Path(str(request["runtime_root"]))
        model_root = Path(str(request["model_dir"]))
        phase = "package_load"
        loader, nodes, runtime = _load_package(runtime_root)
        decode = runtime.decode_codes

        def decode_audio(codec, codes):
            nonlocal phase
            phase = "decoding"
            _write_status(status, {
                "status": "ready", "phase": phase, "audio_started": False,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            })
            if codes.device.type == "cuda":
                import torch
                # This worker renders once. The generator is no longer needed,
                # and retaining it competes with the codec for device memory.
                with torch.cuda.device(codes.device):
                    torch.cuda.synchronize()
                    bundle.model = None
                    bundle.patchers.clear()
                    gc.collect()
                    torch.cuda.empty_cache()
            return decode(codec, codes)

        runtime.decode_codes = decode_audio
        loader.model_dirs = lambda: [model_root]
        variant = str(request.get("model_variant", "int8_hybrid") or "int8_hybrid")
        try:
            label = getattr(loader, _VARIANT_LABEL_ATTR[variant])
        except (KeyError, AttributeError) as exc:
            raise RuntimeError("BREEZE_MODEL_VARIANT_UNSUPPORTED") from exc
        phase = "model_load"
        if request.get('adapter_dir'):
            spec = importlib.util.spec_from_file_location(
                'olivia_breeze_adapter', Path(__file__).with_name('breeze_adapter.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            register = loader.register_runtime_module

            def register_with_adapter(model, device, **kwargs):
                nonlocal adapter_receipt
                if hasattr(model, 'backbone_model'):
                    adapter_receipt = module.apply_adapter(
                        model, request['adapter_dir'], loader.int8.ConvRotInt8Linear,
                        request.get('adapter', {}).get('sha256'))
                return register(model, device, **kwargs)

            loader.register_runtime_module = register_with_adapter
        bundle = loader.load_breeze_bundle(
            label,
            str(request.get("dtype", "bf16") or "bf16"),
            str(request.get("device", "cuda") or "cuda"),
            str(request.get("attention", "eager") or "eager"),
            False,
            str(request.get("decode_mode", "eager") or "eager"),
        )
        if request.get('adapter_dir') and not adapter_receipt:
            raise ValueError('BREEZE_ADAPTER_INVALID')
        phase = "reference_read"
        reference = _read_reference_audio(Path(str(request["reference_audio"])))
        phase = "reference_encode"
        reference_waveform, reference_rate = runtime.comfy_audio_to_tensor(reference)
        reference_codes = runtime.encode_reference_audio(
            bundle.codec, reference_waveform, reference_rate
        )
        phase = "generation"
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
            cfg_scale=float(request.get("cfg_scale", 1.0)),
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
            progress_callback=progress,
            progress_label=None,
        )
        phase = "audio_write"
        _write_wav(
            output,
            result["waveform"],
            int(result["sample_rate"]),
            float(request.get("gain_db", 0.0)),
        )
        _write_status(
            status,
            {"status": "completed", "phase": "completed", "audio_started": True,
             **({'adapter': adapter_receipt} if adapter_receipt else {})},
        )
    except Exception as exc:
        _write_failure(status, phase, exc)
        raise

    finally:
        if register is not None:
            loader.register_runtime_module = register
        if decode is not None:
            runtime.decode_codes = decode


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
    except Exception as exc:
        _write_failure(args.status, "request", exc)
        return 2
    try:
        _synthesize(request, args.output, args.status)
    except Exception:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
