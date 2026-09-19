"""Package the existing licensed voice assets with a separate AMD test harness."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import urllib.request
import zipfile

REPO = Path(__file__).resolve().parents[1]
CK_URL = "https://files.pythonhosted.org/packages/27/d1/e53410260b81610233cb56c2fac1a9f3d39887be3cbb983cd8baa6a07528/comfy_kitchen-0.2.31-py3-none-any.whl"
CK_SHA = "5117946c30f308cfc73b9c26f723ae3918308bd090e57a8eae298406934aabd6"


def build(app, stage, output, bootstrap_site):
    if stage.exists() or output.exists():
        raise ValueError("Use a new staging directory and output file")
    stage.mkdir(parents=True)
    source = app / "data/capabilities/video/ordinary_video/breeze"
    config = json.loads((app / "data/capabilities/video/generated/tts_local.json").read_text(encoding="utf-8"))
    settings = config["settings"]
    options = settings["provider_options"]
    python = stage / "python"
    python.mkdir()
    for path in (source / "python").iterdir():
        if path.is_file():
            shutil.copy2(path, python / path.name)
    (python / "python312._pth").write_text("python312.zip\n.\n..\nLib/site-packages\nimport site\n", encoding="ascii")
    site = python / "Lib/site-packages"
    site.mkdir(parents=True)
    for path in (source / "python/Lib/site-packages").iterdir():
        name = path.name.lower().replace("-", "_")
        if name.startswith(("torch", "functorch", "comfy_kitchen", "~")) or name == "__pycache__":
            continue
        if path.is_dir():
            shutil.copytree(path, site / path.name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(path, site / path.name)
    for prefix in ("pip", "wheel"):
        for path in bootstrap_site.glob(prefix + "*"):
            if path.is_dir() and not (site / path.name).exists():
                shutil.copytree(path, site / path.name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if not (site / "pip/__main__.py").is_file() or not (site / "wheel").is_dir():
        raise ValueError("Bootstrap pip and wheel are required")
    ck = stage / "comfy-kitchen.whl"
    urllib.request.urlretrieve(CK_URL, ck)
    if hashlib.sha256(ck.read_bytes()).hexdigest() != CK_SHA:
        raise ValueError("COMFY_KITCHEN_HASH_MISMATCH")
    with zipfile.ZipFile(ck) as archive:
        archive.extractall(site)
    ck.unlink()
    for name in ("amd_voice_probe.py",):
        shutil.copy2(REPO / "tools" / name, stage / name)
    for name in ("external_breeze_worker.py", "breeze_adapter.py"):
        shutil.copy2(REPO / "tts" / name, stage / name)
    shipped = {"settings": {
        "provider": "breeze_tts2", "runtime_root": "assets/runtime", "model_dir": "assets/model",
        "reference_audio": "assets/reference.wav", "reference_text": settings["reference_text"],
        "provider_options": {"adapter_dir": "assets/adapter"}}}
    (stage / "voice-config.json").write_text(json.dumps(shipped, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, argument in (("1-Start-AMD-Voice-Test.cmd", ""), ("2-Diagnose-Only.cmd", "--diagnose-only")):
        (stage / name).write_text('@echo off\ncd /d "%~dp0"\n"%~dp0python\\python.exe" -I -X utf8 "%~dp0amd_voice_probe.py" '
                                + argument + '\npause\n', encoding="ascii")
    shutil.copy2(REPO / "docs/AMD-voice-preview.md", stage / "使用说明.md")
    manifest = {"kit": "amd-voice-preview-1", "base": "5fcbef3c81c05db9985d9a562b1453a58f777bea",
                "hardware_verified": False, "files": {}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
        def add(path, name):
            digest = hashlib.sha256()
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_STORED if path.stat().st_size > 64 * 1024**2 else zipfile.ZIP_DEFLATED
            with path.open("rb") as src, archive.open(info, "w", force_zip64=True) as dst:
                while block := src.read(4 * 1024 * 1024):
                    digest.update(block)
                    dst.write(block)
            manifest["files"][name] = {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}
        for path in stage.rglob("*"):
            if path.is_file():
                add(path, path.relative_to(stage).as_posix())
        for folder, dest in ((source / "runtime", "assets/runtime"),
                             (Path(settings["model_dir"]), "assets/model"),
                             (Path(options["adapter_dir"]), "assets/adapter")):
            for path in folder.rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts and path.suffix not in (".pyc", ".log"):
                    add(path, dest + "/" + path.relative_to(folder).as_posix())
        add(Path(settings["reference_audio"]), "assets/reference.wav")
        archive.writestr("package-manifest.json", json.dumps(manifest, indent=2))
    with output.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    output.with_suffix(".zip.sha256").write_text(digest + "  " + output.name + "\n", encoding="ascii")
    return output


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--app", type=Path, required=True)
    p.add_argument("--stage", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--bootstrap-site", type=Path, required=True)
    a = p.parse_args()
    print(build(a.app, a.stage, a.output, a.bootstrap_site))
