"""Standalone, opt-in ROCm voice qualification; never writes application state."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
import traceback
import wave
import zipfile

ROOT = Path(__file__).resolve().parent
TEXTS = (
    "今天过得怎么样？我刚坐下来，想和你说几句话。别急，你慢慢说，我听着呢。",
    "刚才收拾桌子，发现那本书还停在昨天翻到的地方。我本来只想再看两页，结果一抬头，天都暗了。你今天有没有遇到什么有意思的事？小事也行。等你有空了，给我写封信吧。",
)
AMD_BASE = "https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/"
AMD_FILES = (
    "rocm_sdk_core-7.2.1-py3-none-win_amd64.whl",
    "rocm_sdk_devel-7.2.1-py3-none-win_amd64.whl",
    "rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl",
    "rocm-7.2.1.tar.gz",
    "torch-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl",
    "torchaudio-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl",
)


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def failure(exc):
    # Never export exception messages, source text, locals, environment or full paths.
    message = str(exc).lower()
    category = "runtime"
    for needle, code in (("out of memory", "gpu_memory"), ("no kernel image", "gpu_arch"),
                         ("invalid device function", "gpu_arch"), ("dll", "dll_import"),
                         ("disk", "disk"), ("timeout", "timeout")):
        if needle in message:
            category = code
            break
    result = {"category": category, "type": type(exc).__name__, "frames": [
        {"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
        for f in traceback.extract_tb(exc.__traceback__)[-12:]]}
    if isinstance(exc, ModuleNotFoundError) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,100}", exc.name or ""):
        result["missing_module"] = exc.name
    if str(exc) in {"BUNDLED_PYTHON_REQUIRED", "ROCM_RUNTIME_REQUIRED", "AMD_DEVICE_UNAVAILABLE",
                    "BREEZE_CONFIGURATION_REQUIRED", "BREEZE_ADAPTER_REQUIRED", "BREEZE_ADAPTER_INVALID",
                    "INSTALL_ROCM_FAILED", "INSTALL_TORCH_FAILED"}:
        result["code"] = str(exc)
    return result


def hardware():
    result = {"os": platform.system(), "build": platform.version(),
              "python": platform.python_version(), "gpus": []}
    if os.name == "nt":
        command = "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion | ConvertTo-Json -Compress"
        try:
            p = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                               capture_output=True, timeout=30)
            value = json.loads(p.stdout.decode("utf-8-sig", errors="replace"))
            result["gpus"] = value if isinstance(value, list) else [value]
        except Exception as exc:
            result["hardware_error"] = failure(exc)
    return result


def eligibility(info):
    if info["os"] != "Windows":
        return "WINDOWS_REQUIRED"
    try:
        if int(info["build"].split(".")[2]) < 22000:
            return "WINDOWS_11_REQUIRED"
    except (ValueError, IndexError):
        return "WINDOWS_BUILD_UNKNOWN"
    if not any(re.search(r"AMD|Radeon", x.get("Name", ""), re.I) for x in info["gpus"]):
        return "AMD_GPU_NOT_DETECTED"
    return None


def make_request(config, text):
    settings = json.loads(config.read_text(encoding="utf-8"))["settings"]
    options = settings["provider_options"]
    for key in ("runtime_root", "model_dir", "reference_audio"):
        path = Path(settings[key])
        settings[key] = str(path if path.is_absolute() else config.parent / path)
    path = Path(options.get("adapter_dir", ""))
    options["adapter_dir"] = str(path if path.is_absolute() else config.parent / path)
    if settings.get("provider") != "breeze_tts2":
        raise ValueError("BREEZE_CONFIGURATION_REQUIRED")
    from breeze_adapter import adapter_metadata
    adapter = adapter_metadata(options.get("adapter_dir", ""))
    if not adapter:
        raise ValueError("BREEZE_ADAPTER_REQUIRED")
    request = {key: settings[key] for key in
               ("runtime_root", "model_dir", "reference_audio", "reference_text")}
    for key in ("runtime_root", "model_dir", "reference_audio"):
        if not Path(request[key]).exists():
            raise FileNotFoundError(key)
    request.update(text=text, instruction="", adapter=adapter,
                   adapter_dir=options["adapter_dir"], model_variant="int8_hybrid",
                   dtype="bf16", device="cuda", attention="eager", decode_mode="eager",
                   cfg_scale=1.0, seed=200717, max_new_tokens=650,
                   temperature=0.9, top_k=50, top_p=1.0, repetition_penalty=1.1,
                   depth_temperature=0.9, depth_top_k=50, depth_top_p=1.0,
                   audio_only_unbounded=False)
    return request


def child(config, output, sample, allow_cuda=False):
    report = {"sample": sample, "phase": "torch_import", "status": "failed"}
    status = output / f"worker-{sample}.json"
    def phase(value):
        report["phase"] = value
        dump(output / f"result-{sample}.json", report)
    phase("torch_import")
    try:
        import torch
        report.update(torch=torch.__version__, hip=torch.version.hip, cuda=torch.version.cuda)
        if not torch.version.hip and not allow_cuda:
            raise RuntimeError("ROCM_RUNTIME_REQUIRED")
        if not torch.cuda.is_available():
            raise RuntimeError("AMD_DEVICE_UNAVAILABLE")
        device = torch.cuda.get_device_properties(0)
        report.update(gpu=device.name, vram_bytes=device.total_memory,
                      architecture=getattr(device, "gcnArchName", ""))
        phase("bf16_operator")
        a = torch.ones((64, 64), device="cuda", dtype=torch.bfloat16)
        assert torch.isfinite(a @ a).all().item()
        del a
        import comfy_kitchen as ck
        phase("int8_convrot_operator")
        # Safe reference backend first; no CUDA extension or unvalidated Triton dispatch.
        from comfy_kitchen.backends.eager.quantization import quantize_int8_convrot_weight
        with ck.use_backend("eager"):
            w = torch.randn((256, 256), device="cuda", dtype=torch.bfloat16)
            x = torch.randn((1, 256), device="cuda", dtype=torch.bfloat16)
            q, scale = quantize_int8_convrot_weight(w, 256, 0)
            y = ck.int8_linear(x, q, scale, out_dtype=torch.bfloat16, convrot=True,
                               convrot_groupsize=256)
            assert y.shape == (1, 256) and torch.isfinite(y).all().item()
            report["int8_backend"] = "eager"
            del w, x, q, scale, y
            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()
            phase("voice")
            import external_breeze_worker as worker
            started = time.monotonic()
            worker._synthesize(make_request(config, TEXTS[sample]),
                               output / f"voice-{sample + 1}.wav", status)
            elapsed = time.monotonic() - started
        with wave.open(str(output / f"voice-{sample + 1}.wav")) as audio:
            duration = audio.getnframes() / audio.getframerate()
        report.update(status="completed", phase="completed", elapsed_seconds=round(elapsed, 3),
                      audio_seconds=round(duration, 3), rtf=round(elapsed / duration, 3),
                      peak_allocated_bytes=torch.cuda.max_memory_allocated(),
                      peak_reserved_bytes=torch.cuda.max_memory_reserved())
        if status.is_file():
            raw = json.loads(status.read_text(encoding="utf-8"))
            report["worker"] = worker.project_worker_status(raw)
            report["adapter_loaded"] = bool(raw.get("adapter"))
            if raw.get("limit_reached"):
                report["status"] = "incomplete_token_limit"
    except Exception as exc:
        report["error"] = failure(exc)
        if status.is_file():
            from external_breeze_worker import project_worker_status
            report["worker"] = project_worker_status(json.loads(status.read_text(encoding="utf-8")))
    finally:
        dump(output / f"result-{sample}.json", report)
    return 0 if report["status"] == "completed" else 2


def install_runtime(output):
    if Path(sys.executable).resolve() != (ROOT / "python/python.exe").resolve():
        raise RuntimeError("BUNDLED_PYTHON_REQUIRED")
    if shutil.disk_usage(ROOT).free < 18 * 1024**3:
        raise RuntimeError("At least 18 GiB free space required")
    env = dict(os.environ)
    for key in list(env):
        if key.upper().startswith("PIP_"):
            del env[key]
    env.update(PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1",
               TEMP=str(output), TMP=str(output))
    # Network installation is confined to the bundled Python, never the app Python.
    for label, files in (("rocm", AMD_FILES[:4]), ("torch", AMD_FILES[4:])):
        print(f"Installing {label}; first run downloads several GB. Please wait.", flush=True)
        args = [sys.executable, "-I", "-m", "pip", "--isolated", "install", "--no-cache-dir",
                "--no-deps", "--no-build-isolation", "--force-reinstall", "--retries", "3", "--timeout", "60",
                *[AMD_BASE + name for name in files]]
        # Keep raw installer output local; it is explicitly excluded from export.
        with (output / f"local-install-{label}.log").open("w", encoding="utf-8") as log:
            p = subprocess.run(args, stdout=log, stderr=log, env=env, timeout=7200)
        if p.returncode:
            raise RuntimeError(f"INSTALL_{label.upper()}_FAILED")
    (ROOT / "runtime-ready.txt").write_text("rocm7.2.1-torch2.9.1", encoding="ascii")


def export_report(output, report):
    # Explicit allowlist: never zip source app data, config, reference audio, raw logs.
    dump(output / "diagnostic.json", report)
    archive = output / "AMD-voice-diagnostic.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(output / "diagnostic.json", "diagnostic.json")
        z.writestr("test-texts.json", json.dumps(TEXTS, ensure_ascii=False))
        for i in (1, 2):
            audio = output / f"voice-{i}.wav"
            if audio.is_file():
                z.write(audio, audio.name)
    return archive


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnose-only", action="store_true")
    parser.add_argument("--child", type=int, choices=(0, 1))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-cuda-control", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child is not None:
        return child(args.config, args.output, args.child, args.allow_cuda_control)
    output = ROOT / "results" / time.strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema": 1, "kit": "amd-voice-preview-1", "hardware": hardware(),
              "status": "failed", "tests": [], "stage": "preflight"}
    try:
        reason = eligibility(report["hardware"])
        if not reason and len(str(ROOT.resolve())) > 50:
            reason = "USE_SHORT_FOLDER_PATH"
        if reason:
            report["reason"] = reason
        elif not args.diagnose_only:
            config = ROOT / "voice-config.json"
            make_request(config, TEXTS[0])  # Validate before downloading anything.
            report["stage"] = "runtime_install"
            if not (ROOT / "runtime-ready.txt").is_file():
                install_runtime(output)
            report["stage"] = "inference"
            for sample in range(2):
                print(f"Voice test {sample + 1}/2 (timeout: 15 minutes).", flush=True)
                env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                           HF_HOME=str(output / "hf-cache"), TEMP=str(output), TMP=str(output))
                # Child process contains GPU faults; parent still exports diagnostics.
                process_failed = False
                timed_out = False
                try:
                    with (output / f"local-worker-{sample}.log").open("w", encoding="utf-8") as log:
                        p = subprocess.run([sys.executable, "-I", str(Path(__file__)), "--child", str(sample),
                            "--config", str(config), "--output", str(output)], env=env,
                            stdout=log, stderr=log, timeout=900)
                    item = {"sample": sample, "exit_code": p.returncode}
                    process_failed = p.returncode != 0
                except subprocess.TimeoutExpired:
                    item = {"sample": sample, "status": "timeout"}
                    timed_out = True
                result = output / f"result-{sample}.json"
                if result.exists():
                    item.update(json.loads(result.read_text(encoding="utf-8")))
                status = output / f"worker-{sample}.json"
                if status.exists():
                    from external_breeze_worker import project_worker_status
                    item["worker"] = project_worker_status(json.loads(status.read_text(encoding="utf-8")))
                if timed_out:
                    item["status"] = "timeout"
                elif process_failed:
                    item["status"] = "process_failed"
                report["tests"].append(item)
                if item.get("status") != "completed":
                    break
            report["status"] = "completed" if len(report["tests"]) == 2 and all(
                x.get("status") == "completed" for x in report["tests"]) else "failed"
        else:
            report["status"] = "diagnosed_only"
    except Exception as exc:
        report["error"] = failure(exc)
    finally:
        archive = export_report(output, report)
        print(f"Status: {report['status']}. Send this file back: {archive}", flush=True)
    return 0 if report["status"] in ("completed", "diagnosed_only") else 2


if __name__ == "__main__":
    raise SystemExit(main())
