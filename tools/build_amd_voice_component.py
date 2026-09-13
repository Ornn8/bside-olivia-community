"""Build the native offline voice component from a prepared isolated ROCm tree."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from installer.build_media_component import build


def package(root, output):
    python = root / "python/python.exe"
    # Importability is distinct from AMD GPU acceptance; no GPU is required to package.
    subprocess.run([str(python), "-I", "-c", "import torch, torchaudio, comfy_kitchen; "
                    "assert torch.__version__ == '2.9.1+rocm7.2.1'; assert torch.version.hip"], check=True)
    value = json.loads((root / "voice-config.json").read_text(encoding="utf-8"))
    settings = value["settings"]
    settings.update(license_id="BreezeBlue-Research-and-Non-Commercial-1.0", language="zh",
                    fallback="text", fp16=False)
    settings["provider_options"].update(external_python="python/python.exe", runtime_backend="rocm",
        model_license_path="assets/model/LICENSE", model_variant="int8_hybrid", device="cuda",
        dtype="bf16", attention="eager", decode_mode="eager", cfg_scale=1.0, seed=200717)
    with tempfile.TemporaryDirectory(prefix="amd-component-", dir=output.parent) as temporary:
        config = Path(temporary) / "tts.json"
        config.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return build({"component": "voice", "version": "amd-1.2.3",
            "environment": {"OLIVIA_TTS_CONFIG": "tts.json"},
            "mounts": [{"source": str(root / "python"), "target": "python"},
                       {"source": str(root / "assets"), "target": "assets"},
                       {"source": str(config), "target": "tts.json"}]}, output)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--prepared-runtime", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    a.output.parent.mkdir(parents=True, exist_ok=True)
    print(json.dumps(package(a.prepared_runtime, a.output)))
