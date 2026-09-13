from types import SimpleNamespace

from tts.breeze import BreezeTTS2Provider
from tts.contracts import TTSConfig
from tts.external_breeze_worker import _worker_error_code, project_worker_status


def test_rocm_request_survives_audio_config_projection():
    config = TTSConfig(provider="breeze_tts2", provider_options={"runtime_backend": "rocm"})
    request = BreezeTTS2Provider(config).performance_request(SimpleNamespace(spoken_text="测试"))
    assert request["runtime_backend"] == "rocm"
    assert request["attention"] == request["decode_mode"] == "eager"


def test_rocm_runtime_probe_does_not_require_nvidia_cuda(tmp_path, monkeypatch):
    import runtime.media.music_reply as m
    executable = tmp_path / "python.exe"
    executable.touch()
    seen = []
    monkeypatch.setattr(m, "_run_runtime_probe", lambda command, **kwargs: seen.append(command) or True)
    assert m._python_runtime_ready(executable, cwd=tmp_path, imports=("torch",),
        accepted_torch_versions=("2.9.1+rocm7.2.1",), torch_backend="rocm")
    assert "assert torch.version.hip" in seen[0][-1]
    assert "assert torch.version.cuda" not in seen[0][-1]


def test_rocm_gpu_health_probes_selected_python(tmp_path, monkeypatch):
    import runtime.media.music_reply as m
    executable = tmp_path / "python.exe"
    executable.touch()
    seen = []
    monkeypatch.setattr(m, "_run_runtime_probe", lambda command, **kwargs: seen.append(command) or True)
    assert m._breeze_hardware_status(executable, backend="rocm") == (True, None)
    assert "torch.version.hip" in seen[0][-1]
    assert "total_memory" in seen[0][-1]


def test_hip_failure_is_exportable_without_exception_text():
    error = RuntimeError("HIP out of memory private-path")
    assert _worker_error_code(error) == "BREEZE_ROCM_OUT_OF_MEMORY"
    assert project_worker_status({"error_code": _worker_error_code(error)}) == {
        "error_code": "BREEZE_ROCM_OUT_OF_MEMORY"}


def test_amd_component_install_and_relocation_preserves_backend(tmp_path, monkeypatch):
    import json
    import zipfile
    from pathlib import Path
    import video_capability_install as install
    from runtime.media.component_packages import MediaComponents
    root = tmp_path / "source"
    for name in ("python/python.exe", "runtime/__init__.py", "model/LICENSE", "reference.wav", "adapter/adapter_config.json"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    config = {"settings": {"provider": "breeze_tts2", "runtime_root": "runtime", "model_dir": "model",
        "reference_audio": "reference.wav", "provider_options": {"external_python": "python/python.exe",
        "model_license_path": "model/LICENSE", "adapter_dir": "adapter", "runtime_backend": "rocm"}}}
    (root / "tts.json").write_text(json.dumps(config))
    install.write_runtime_root_manifest(root, version="component.voice.amd123", environment={"OLIVIA_TTS_CONFIG": "tts.json"})
    archive = tmp_path / "amd.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for path in root.rglob("*"):
            if path.is_file():
                z.write(path, path.relative_to(root))
    monkeypatch.setattr(install, "_portable_python_runtime", lambda *args: True)
    manager = MediaComponents(tmp_path / "data")
    assert manager.install(archive, expected_component="voice") == "APPLIED"
    moved = tmp_path / "moved"
    (tmp_path / "data").rename(moved)
    manager = MediaComponents(moved)
    installed = json.loads(Path(manager.environment()["OLIVIA_TTS_CONFIG"]).read_text())
    options = installed["settings"]["provider_options"]
    assert options["runtime_backend"] == "rocm"
    assert str(moved) in options["external_python"]
    assert "AMD" in next(x for x in manager.status()["items"] if x["id"] == "voice")["label"]


def test_old_cuda_environment_does_not_override_amd_component(tmp_path, monkeypatch):
    import json
    import runtime.reply.reply_media as media
    config = {"settings": {"provider": "breeze_tts2", "runtime_root": str(tmp_path), "model_dir": str(tmp_path),
        "reference_audio": str(tmp_path / "ref.wav"), "reference_text": "synthetic",
        "provider_options": {"runtime_backend": "rocm", "external_python": str(tmp_path / "amd.exe")}}}
    path = tmp_path / "tts.json"
    path.write_text(json.dumps(config))
    result = media._tts_config(path, tmp_path, env={"OLIVIA_BREEZE_TTS_PYTHON": str(tmp_path / "cuda.exe")})
    assert result.provider_options["external_python"] == str(tmp_path / "amd.exe")
