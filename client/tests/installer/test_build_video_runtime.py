from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import zipfile

import pytest
import installer.build_video_runtime as builder

_COMPONENTS = ("breeze", "latentsync", "minimax", "roformer")

def _roots(tmp_path: Path) -> dict[str, Path]:
    result = {}
    for component in _COMPONENTS:
        root = (tmp_path / "portable" / component).resolve()
        (root / "python").mkdir(parents=True)
        (root / "site-packages").mkdir()
        (root / "python/python.exe").write_bytes(f"{component}-python".encode())
        (root / "python/LICENSE.txt").write_text("python license must not identify the component\n", encoding="utf-8")
        (root / f"site-packages/{component}.py").write_text(f"COMPONENT={component!r}\n", encoding="utf-8")
        (root / "component-source.py").write_text("must stay in the 26 GiB bundle\n", encoding="utf-8")
        license_path = root / f"site-packages/olivia_upstream/{component}/LICENSE"
        license_path.parent.mkdir(parents=True)
        license_path.write_text(f"{component} license\n", encoding="utf-8")
        (root / "NOTICE").write_text(f"{component} notice\n", encoding="utf-8")
        for relative, content in (("site-packages/distutils-precedence.pth", b"import _distutils_hack\n"), ("site-packages/sentencepiece/package_data/runtime.bin", b"runtime data"), ("site-packages/chardet/models/runtime.bin", b"runtime data"), ("site-packages/chardet/models/__init__.py", b""), ("site-packages/chardet/models/training_metadata.yaml", b"safe: true\n"), ("site-packages/transformers/models/runtime.pyc", b"compiled"), ("site-packages/modelscope/source/runtime.pyx", b"pass\n"), ("site-packages/lightning/useLightningState.ts", b"export const ready = true;\n")):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(content)
        result[component] = root
    return result
def _bom(tmp_path: Path, roots: dict[str, Path]) -> Path:
    components = {}
    for component, root in roots.items():
        paths = [root / "NOTICE"] + [path for folder in (root / "python", root / "site-packages") for path in folder.rglob("*") if path.is_file()]
        files = [{"path": path.relative_to(root).as_posix(), "size_bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in sorted(paths)]
        indexed = {item["path"]: item for item in files}
        components[component] = {
            "upstream": f"{'http' if component == 'breeze' else 'https'}://example.invalid/{component}", "revision": "1" * 40,
            "tree_sha256": hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "dependencies": [f"{component}==1.0"], "files": files,
            "license": {key: next(item for path, item in indexed.items() if path.startswith("site-packages/olivia_upstream/") and path.endswith("/LICENSE"))[key] for key in ("path", "sha256")},
            "notice": {key: indexed["NOTICE"][key] for key in ("path", "sha256")},
        }
    path = (tmp_path / "build-input-bom.json").resolve()
    path.write_text(json.dumps({"schema_version": "olivia.video-runtime-build-inputs.v1", "components": components}), encoding="utf-8")
    return path
def _allow_probes(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[list[str], dict[str, object]]] | None = None) -> None:
    def run(command, **_kwargs):
        if calls is not None: calls.append((command, _kwargs))
        return builder.subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(builder.subprocess, "run", run)
def _build(tmp_path: Path, roots: dict[str, Path], bom: Path, version: str = "test") -> Path:
    return builder.build_video_runtime_archive(version=version, output_directory=(tmp_path / "out").resolve(), component_roots=roots, build_input_bom=bom)
def test_duplicate_component_roots_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots = _roots(tmp_path); roots["roformer"] = roots["minimax"]
    _allow_probes(monkeypatch)
    with pytest.raises(builder.VideoRuntimeBuildError, match="VIDEO_RUNTIME_BUILD_COMPONENT_DUPLICATE"):
        _build(tmp_path, roots, _bom(tmp_path, roots))

def test_builds_verified_v2_archive_and_confines_four_role_probes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots, calls = _roots(tmp_path), []; monkeypatch.setenv("PATH", r"C:\host-only-poison"); _allow_probes(monkeypatch, calls)
    archive = _build(tmp_path, roots, _bom(tmp_path, roots))
    with zipfile.ZipFile(archive) as payload:
        manifest_bytes = payload.read("runtime-manifest.json")
        manifest, names = json.loads(manifest_bytes), set(payload.namelist())
        assert manifest_bytes == (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        assert payload.testzip() is None
        assert all((len(content := payload.read(item["path"])), hashlib.sha256(content).hexdigest()) == (item["size_bytes"], item["sha256"]) for item in manifest["files"])
    commands, environments = [call[0] for call in calls], [call[1]["env"] for call in calls]
    scripts = "\n".join(command[-2] for command in commands)
    assert len(commands) == len({command[-1] for command in commands}) == 8 and len(set(manifest["environment"].values())) == 4
    assert all(command[1:3] == ["-I", "-B"] and Path(command[0]).is_relative_to(Path(command[-1])) for command in commands)
    assert all(r"C:\host-only-poison" not in environment["PATH"] and str(Path(command[0]).parent) in environment["PATH"] for command, environment in zip(commands, environments, strict=True))
    assert all(call[1]["timeout"] == 120 and environment["PYTHONDONTWRITEBYTECODE"] == "1" for call, environment in zip(calls, environments, strict=True))
    assert all(name in scripts for name in ("transformers", "soundfile", "whisper", "latentsync", "comfy_extras.nodes_minimax_music", "mel_band_roformer.inference", "device='cuda'"))
    assert all(any(name.startswith(f"{component}/runtime/") for name in names) for component in roots)

def test_rejects_bare_python_without_locked_bom(tmp_path: Path) -> None:
    with pytest.raises(builder.VideoRuntimeBuildError, match="VIDEO_RUNTIME_BUILD_BOM_REQUIRED"):
        _build(tmp_path, _roots(tmp_path), None)  # type: ignore[arg-type]

@pytest.mark.parametrize("fault", ("source-drift", "missing-license-hash", "foreign-license", "main", "latest", "userinfo", "query", "fragment", "ftp", "empty-host"))
def test_bom_provenance_and_inventory_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    roots = _roots(tmp_path); bom = _bom(tmp_path, roots)
    expected = "VIDEO_RUNTIME_BUILD_BOM_MISMATCH"
    if fault == "source-drift":
        (roots["breeze"] / "site-packages/breeze.py").write_text("drift\n")
    else:
        payload = json.loads(bom.read_text(encoding="utf-8"))
        if fault == "missing-license-hash": payload["components"]["breeze"]["license"].pop("sha256")
        elif fault == "foreign-license": payload["components"]["breeze"]["license"] = next({key: item[key] for key in ("path", "sha256")} for item in payload["components"]["breeze"]["files"] if item["path"] == "python/LICENSE.txt")
        elif fault in {"main", "latest"}: payload["components"]["breeze"]["revision"] = fault
        else: payload["components"]["breeze"]["upstream"] = {"userinfo": "https://secret-token@example.invalid/breeze", "query": "https://example.invalid/breeze?token=private", "fragment": "https://example.invalid/breeze#private", "ftp": "ftp://example.invalid/breeze", "empty-host": "https:///breeze"}[fault]
        bom.write_text(json.dumps(payload), encoding="utf-8")
        expected = "VIDEO_RUNTIME_BUILD_BOM_INVALID"
    monkeypatch.setattr(builder, "_probe_environments", lambda *_args: pytest.fail("BOM validation must precede probe"))
    with pytest.raises(builder.VideoRuntimeBuildError, match=expected): _build(tmp_path, roots, bom)
    assert not list((tmp_path / "out").glob("*.zip"))

@pytest.mark.parametrize(
    ("relative", "size", "error"),
    (("site-packages/pkg/renamed-private.opus", 1, "PRIVATE_MEDIA"), ("site-packages/pkg/private.mp4", 1, "PRIVATE_MEDIA"), ("site-packages/pkg/models/config.json", 1, "BUNDLE_CONTENT"), ("python/models/runtime.py", 1, "BUNDLE_CONTENT"), ("site-packages/pkg/data/model.pt", 1, "BUNDLE_CONTENT"), ("site-packages/pkg/data/model.bin", 1, "BUNDLE_CONTENT"), ("site-packages/not-site-packages/sentencepiece/package_data/model.bin", 1, "BUNDLE_CONTENT")),
)
def test_locked_inventory_rejects_audio_recursive_models_and_weights(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str, size: int, error: str) -> None:
    roots = _roots(tmp_path); target = roots["minimax"] / relative
    target.parent.mkdir(parents=True); target.write_bytes(b"x" * size)
    bom = _bom(tmp_path, roots); _allow_probes(monkeypatch)
    with pytest.raises(builder.VideoRuntimeBuildError, match=f"VIDEO_RUNTIME_BUILD_{error}_FORBIDDEN"):
        _build(tmp_path, roots, bom)

@pytest.mark.parametrize("header", (b"RIFF\x00\x00\x00\x00WAVE", b"FORM\x00\x00\x00\x00AIFF", b"RIFF\x00\x00\x00\x00AVI ", b"fLaC", b"OggS", b"ID3", b"FLV\x01", b"\xff\xfb", b"\x00\x00\x00\x18ftypisom", b"\x1a\x45\xdf\xa3", b"G" + b"\x00" * 187 + b"G" + b"\x00" * 187 + b"G", b"G" + b"\x00" * 187 + b"G" + b"\x00" * 187 + b"G\x00"))
def test_renamed_common_media_signatures_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header: bytes) -> None:
    roots = _roots(tmp_path); (roots["minimax"] / ("site-packages/private.ts" if len(header) == 378 else "site-packages/private.dat")).write_bytes(header + b"private")
    _allow_probes(monkeypatch)
    with pytest.raises(builder.VideoRuntimeBuildError, match="VIDEO_RUNTIME_BUILD_PRIVATE_MEDIA_FORBIDDEN"):
        _build(tmp_path, roots, _bom(tmp_path, roots))

@pytest.mark.parametrize(("relative", "content"), (("site-packages/model.pth", b"PK\x03\x04binary"), pytest.param("site-packages/large.pth", b"x" * (64 * 1024 + 1), id="oversize"), ("python/config.pth", b"relative-path\n")))
def test_pth_rejects_binary_oversize_or_non_site_packages_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str, content: bytes) -> None:
    roots = _roots(tmp_path); (roots["minimax"] / relative).write_bytes(content)
    _allow_probes(monkeypatch)
    with pytest.raises(builder.VideoRuntimeBuildError, match="VIDEO_RUNTIME_BUILD_BUNDLE_CONTENT_FORBIDDEN"):
        _build(tmp_path, roots, _bom(tmp_path, roots))
@pytest.mark.parametrize("component", _COMPONENTS)
def test_failed_component_cuda_probe_leaves_no_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str) -> None:
    roots = _roots(tmp_path)
    monkeypatch.setattr(builder.subprocess, "run", lambda command, **_kwargs: builder.subprocess.CompletedProcess(command, 1 if builder._COMPONENT_IMPORTS[component][-1] in command[-2] else 0))
    monkeypatch.setattr(builder.shutil, "copytree", lambda *_args, **_kwargs: pytest.fail("probe must precede copy"))
    with pytest.raises(builder.VideoRuntimeBuildError, match=f"VIDEO_RUNTIME_BUILD_{component.upper()}_NOT_PORTABLE"):
        _build(tmp_path, roots, _bom(tmp_path, roots), "probe-fails")
    assert not list((tmp_path / "out").glob("*.zip"))

def test_verifies_staging_before_atomic_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots, output = _roots(tmp_path), (tmp_path / "out").resolve()
    final = output / "Olivia-video-runtime-atomic.zip"
    _allow_probes(monkeypatch)
    def reject(staging: Path, _sha: str) -> None:
        assert staging != final and not final.exists()
        raise builder.VideoRuntimeBuildError("VERIFY_FAILED")
    monkeypatch.setattr(builder, "_verify_zip", reject)
    with pytest.raises(builder.VideoRuntimeBuildError, match="VERIFY_FAILED"):
        _build(tmp_path, roots, _bom(tmp_path, roots), "atomic")
    assert not final.exists()

@pytest.mark.parametrize("occupied", ("lock", "target"))
def test_cross_process_lock_and_no_overwrite_preserve_existing_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, occupied: str) -> None:
    roots, output = _roots(tmp_path), (tmp_path / "out").resolve(); _allow_probes(monkeypatch)
    output.mkdir()
    target = output / "Olivia-video-runtime-owned.zip"
    path = output / f".{target.name}.lock" if occupied == "lock" else target
    path.write_bytes(b"owner")
    expected = "LOCKED" if occupied == "lock" else "OUTPUT_EXISTS"
    with pytest.raises(builder.VideoRuntimeBuildError, match=f"VIDEO_RUNTIME_BUILD_{expected}"):
        _build(tmp_path, roots, _bom(tmp_path, roots), "owned")
    assert path.read_bytes() == b"owner"

@pytest.mark.skipif(sys.platform != "win32", reason="private runtime builder targets Windows")
def test_build_lock_is_deleted_if_owner_process_exits(tmp_path: Path) -> None:
    lock = (tmp_path / "crash-safe.lock").resolve()
    script = "import os,sys; from pathlib import Path; from installer.build_video_runtime import _open_build_lock; _open_build_lock(Path(sys.argv[1])); os._exit(0)"
    result = builder.subprocess.run([sys.executable, "-c", script, str(lock)], cwd=Path(builder.__file__).resolve().parents[1])
    assert result.returncode == 0 and not lock.exists()

def test_output_inside_input_and_oversize_fail_before_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots = _roots(tmp_path); bom = _bom(tmp_path, roots)
    with pytest.raises(builder.VideoRuntimeBuildError, match="VIDEO_RUNTIME_BUILD_OUTPUT_INVALID"):
        builder.build_video_runtime_archive(version="nested", output_directory=(roots["breeze"] / "out").resolve(), component_roots=roots, build_input_bom=bom)
    monkeypatch.setattr(builder, "_MAX_EXPANDED_BYTES", 1)
    with pytest.raises(builder.VideoRuntimeBuildError, match="VIDEO_RUNTIME_BUILD_TOO_LARGE"):
        _build(tmp_path, roots, bom, "large")

@pytest.mark.parametrize("entry", (("-m", "installer.build_video_runtime"), (str(Path(builder.__file__).resolve()),)))
def test_public_cli_returns_sanitized_stable_json_error(entry: tuple[str, ...]) -> None:
    result = builder.subprocess.run([sys.executable, *entry], cwd=Path(builder.__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 2
    assert json.loads(result.stderr) == {"status": "ERROR", "code": "VIDEO_RUNTIME_BUILD_ARGUMENTS_INVALID"}
