from __future__ import annotations

import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
from types import SimpleNamespace
import wave
import zipfile

import pytest

import installer.build_windows_setup as setup_builder
from installer.build_windows_setup import (
    BUILD_CONTROL_FILES,
    SetupBuildError,
    _git_tracked_files,
    _is_release_file,
    _video_runtime_relative,
    build_windows_setup,
    main as build_setup_main,
    prepare_setup_payload,
)


ROOT = Path(__file__).resolve().parents[2]


def test_setup_builder_direct_entrypoint_reaches_argument_parser() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "installer" / "build_windows_setup.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--video-runtime" in result.stdout


def _write_asset(root: Path, relative: str, content: bytes) -> dict[str, object]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {
        "path": relative.replace("\\", "/"),
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _write_voice_reference(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as target:
        target.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        target.writeframes(b"\x00\x00" * 160)


def _write_video_runtime(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    environment = {
        "OLIVIA_COSYVOICE_PYTHON": "cosyvoice/python/python.exe",
        "OLIVIA_LATENTSYNC_PYTHON": "latentsync/python/python.exe",
        "OLIVIA_MINIMAX_COMFY_PYTHON": "minimax/python/python.exe",
        "OLIVIA_ROFORMER_PYTHON": "roformer/python/python.exe",
    }
    files = {relative: b"x" for relative in [*environment.values(), "fixture.bin"]}
    files["empty.marker"] = b""
    manifest = {
        "schema_version": "olivia.video-runtime-root.v1",
        "version": "fixture",
        "environment": environment,
        "files": [
            {"path": relative, "size_bytes": len(content),
             "sha256": hashlib.sha256(content).hexdigest()}
            for relative, content in files.items()
        ],
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("runtime-manifest.json", json.dumps(manifest))
        for relative, content in files.items():
            archive.writestr(relative, content)


def _write_video_runtime_v2(
    path: Path,
    *,
    cosyvoice_extra: dict[str, bytes] | None = None,
    dedicated_license: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    environment: dict[str, str] = {}
    runtime_files: dict[str, bytes] = {}
    components: dict[str, object] = {}
    environment_keys = {
        "cosyvoice": "OLIVIA_COSYVOICE_PYTHON",
        "latentsync": "OLIVIA_LATENTSYNC_PYTHON",
        "minimax": "OLIVIA_MINIMAX_COMFY_PYTHON",
        "roformer": "OLIVIA_ROFORMER_PYTHON",
    }
    for index, (component, environment_key) in enumerate(environment_keys.items()):
        license_path = (
            f"site-packages/olivia_upstream/{component}/LICENSE.txt"
            if dedicated_license
            else "LICENSE.txt"
        )
        source_files = {
            "python/python.exe": f"python-{component}".encode(),
            "NOTICE.txt": f"notice-{component}".encode(),
            license_path: f"license-{component}".encode(),
        }
        if component == "cosyvoice" and cosyvoice_extra:
            source_files.update(cosyvoice_extra)
        file_records = [
            {
                "path": relative,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for relative, content in source_files.items()
        ]
        file_records.sort(key=lambda item: item["path"].casefold())
        components[component] = {
            "upstream": f"https://example.com/{component}",
            "revision": format(index + 1, "040x"),
            "tree_sha256": hashlib.sha256(
                json.dumps(file_records, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "dependencies": [f"{component}-runtime==1.0"],
            "license": {
                "path": license_path,
                "sha256": hashlib.sha256(source_files[license_path]).hexdigest(),
            },
            "notice": {
                "path": "NOTICE.txt",
                "sha256": hashlib.sha256(source_files["NOTICE.txt"]).hexdigest(),
            },
            "files": file_records,
        }
        prefix = f"{component}/runtime/"
        runtime_files.update(
            {
                prefix + relative: content
                for relative, content in source_files.items()
            }
        )
        environment[environment_key] = prefix + "python/python.exe"
    manifest = {
        "schema_version": "olivia.video-runtime-root.v2",
        "version": "fixture-v2",
        "environment": environment,
        "files": [
            {
                "path": relative,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for relative, content in sorted(runtime_files.items())
        ],
        "build_inputs": {
            "schema_version": "olivia.video-runtime-build-inputs.v1",
            "components": components,
        },
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("runtime-manifest.json", json.dumps(manifest))
        for relative, content in runtime_files.items():
            archive.writestr(relative, content)


def _mutate_video_runtime_manifest(path: Path, mutate) -> None:
    with zipfile.ZipFile(path) as archive:
        files = {
            item.filename: archive.read(item)
            for item in archive.infolist()
            if not item.is_dir() and item.filename != "runtime-manifest.json"
        }
        manifest = json.loads(archive.read("runtime-manifest.json"))
    mutate(manifest)
    compression = (
        zipfile.ZIP_DEFLATED
        if manifest.get("schema_version") == "olivia.video-runtime-root.v2"
        else zipfile.ZIP_STORED
    )
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        archive.writestr("runtime-manifest.json", json.dumps(manifest))
        for relative, content in files.items():
            archive.writestr(relative, content)


def _offline_fixture(root: Path, requirements: bytes) -> None:
    runtime = _write_asset(root, "python.zip", b"runtime")
    runtime["source_url"] = "https://official.example/python.zip"
    pip = _write_asset(root, "pip.whl", b"pip")
    pip.update({"package": "pip", "version": "1"})
    wheel = _write_asset(root, "wheelhouse/core.whl", b"wheel")
    (root / "offline-core-assets.json").write_text(
        json.dumps(
            {
                "schema_version": "fixture.v1",
                "python_runtime": runtime,
                "pip_bootstrap": pip,
                "requirements_sha256": hashlib.sha256(requirements).hexdigest(),
                "wheels": [wheel],
            }
        ),
        encoding="utf-8",
    )


def _video_offline_fixture(source: Path, root: Path) -> dict[str, bytes]:
    files = {
        "ordinary_video/ordinary.bin": b"ordinary",
        "music_video/music.bin": b"music",
    }
    bundles = []
    for bundle_id, relative in (
        ("ordinary_video", "ordinary.bin"),
        ("music_video", "music.bin"),
    ):
        content = files[f"{bundle_id}/{relative}"]
        target = root / bundle_id / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        bundles.append(
            {
                "id": bundle_id,
                "label": bundle_id,
                "status": "FIXED",
                "requires_gpu": True,
                "dependencies": [],
                "files": [
                    {
                        "id": f"{bundle_id}-fixture",
                        "path": relative,
                        "size_bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "license": "MIT",
                        "redistributable": True,
                        "sources": {"official": f"https://example.com/{relative}"},
                    }
                ],
            }
        )
    manifest = source / "installer/video-capability-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "olivia.video-capability-bom.v1",
                "version": "fixture-video",
                "bundles": bundles,
            }
        ),
        encoding="utf-8",
    )
    return files


def _voice_setup_fixture(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    source, offline = tmp_path / "source", tmp_path / "offline"
    installer = source / "installer"
    installer.mkdir(parents=True)
    (installer / "Install.ps1").write_text("install", encoding="utf-8")
    (installer / "activate_private_video.py").write_text(
        "# private video activation entrypoint\n", encoding="utf-8"
    )
    requirements = b"locked requirements"
    (installer / "runtime-requirements.txt").write_bytes(requirements)
    reference = tmp_path / "distributor" / "olivia-reference.wav"
    _write_voice_reference(reference)
    _video_offline_fixture(
        source, tmp_path / "distributor" / "Olivia-video-offline-fixture"
    )
    _offline_fixture(offline, requirements)
    monkeypatch.setattr(
        "installer.build_windows_setup._git_tracked_files",
        lambda _source: {
            "installer/Install.ps1",
            "installer/activate_private_video.py",
            "installer/runtime-requirements.txt",
            "installer/video-capability-manifest.json",
            *BUILD_CONTROL_FILES,
        },
    )
    monkeypatch.setattr("installer.build_windows_setup._git_dirty_files", lambda _: set())
    return source, offline, reference


def _prepare_private_runtime(
    source: Path,
    offline: Path,
    reference: Path,
    runtime: Path,
    destination: Path,
) -> None:
    prepare_setup_payload(
        source, offline, destination, distribution="private",
        voice_reference=reference,
        video_runtime=runtime,
        video_offline_root=reference.parent / "Olivia-video-offline-fixture",
        validate_schema=False,
    )


def test_prepare_setup_payload_copies_only_tracked_release_files_and_offline_assets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    offline = tmp_path / "offline"
    destination = tmp_path / "payload"
    (source / "installer").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "docs").mkdir()
    (source / "installer" / "Install.ps1").write_text("install", encoding="utf-8")
    (source / "installer" / "assets").mkdir()
    (source / "installer" / "assets" / "olivia.ico").write_bytes(b"icon")
    requirements = b"locked requirements"
    (source / "installer" / "runtime-requirements.txt").write_bytes(requirements)
    (source / "LICENSE").write_text("license", encoding="utf-8")
    (source / "local_server.py").write_text("tracked", encoding="utf-8")
    (source / "untracked.py").write_text("untracked", encoding="utf-8")
    (source / "test_root.py").write_text("hidden", encoding="utf-8")
    (source / "requirements-ci.txt").write_text("hidden", encoding="utf-8")
    (source / "pyproject.toml").write_text("hidden", encoding="utf-8")
    (source / "installer" / "build_windows_setup.py").write_text(
        "hidden", encoding="utf-8"
    )
    (source / "tests" / "test_hidden.py").write_text("hidden", encoding="utf-8")
    (source / "docs" / "internal.md").write_text("hidden", encoding="utf-8")
    _offline_fixture(offline, requirements)
    monkeypatch.setattr(
        "installer.build_windows_setup._git_tracked_files",
        lambda _source: {
            "installer/Install.ps1",
            "installer/assets/olivia.ico",
            "installer/runtime-requirements.txt",
            *BUILD_CONTROL_FILES,
            "LICENSE",
            "local_server.py",
            "test_root.py",
            "requirements-ci.txt",
            "pyproject.toml",
            "installer/build_windows_setup.py",
            "tests/test_hidden.py",
            "docs/internal.md",
        },
    )
    monkeypatch.setattr(
        "installer.build_windows_setup._git_dirty_files", lambda _source: set()
    )

    prepare_setup_payload(source, offline, destination, validate_schema=False)

    assert (destination / "installer" / "Install.ps1").read_text() == "install"
    assert (destination / "installer" / "assets" / "olivia.ico").read_bytes() == b"icon"
    assert (destination / "LICENSE").is_file()
    assert (destination / "local_server.py").is_file()
    assert (destination / "offline" / "offline-core-assets.json").is_file()
    assert not (destination / "untracked.py").exists()
    assert not (destination / "tests").exists()
    assert not (destination / "docs").exists()
    assert not (destination / "test_root.py").exists()
    assert not (destination / "requirements-ci.txt").exists()
    assert not (destination / "pyproject.toml").exists()
    assert not (destination / "installer" / "build_windows_setup.py").exists()


def test_prepare_setup_payload_injects_hash_locked_voice_reference(tmp_path: Path, monkeypatch) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "distributor" / "Olivia-video-runtime-fixture.zip"
    _write_video_runtime(runtime)
    destination = tmp_path / "payload"

    prepare_setup_payload(
        source,
        offline,
        destination,
        distribution="private",
        voice_reference=reference,
        video_runtime=runtime,
        video_offline_root=reference.parent / "Olivia-video-offline-fixture",
        validate_schema=False,
    )

    installed_reference = destination / "offline" / "voice" / "olivia-reference.wav"
    manifest = json.loads((destination / "offline/offline-core-assets.json").read_text())
    assert installed_reference.read_bytes() == reference.read_bytes()
    assert manifest["distribution"] == "private"
    assert manifest["voice_reference"] == {
        "path": "voice/olivia-reference.wav",
        "size_bytes": reference.stat().st_size,
        "sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
        "wave": {"channels": 1, "sample_width_bytes": 2, "sample_rate_hz": 16000,
                 "frame_count": 160, "compression_type": "NONE"},
    }
    assert not (destination / "offline/video-runtime").exists()
    assert manifest["video_runtime"] == {
        "path": "Olivia-video-runtime-private.zip",
        "size_bytes": runtime.stat().st_size,
        "sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
    }
    assert (
        destination / "installer/activate_private_video.py"
    ).read_text(encoding="utf-8") == "# private video activation entrypoint\n"
    input_manifest_path = offline / "offline-core-assets.json"
    input_manifest = json.loads(input_manifest_path.read_text())
    input_manifest.update(
        distribution="private",
        voice_reference=manifest["voice_reference"],
        video_runtime=manifest["video_runtime"],
        video_offline=manifest["video_offline"],
    )
    input_manifest_path.write_text(json.dumps(input_manifest), encoding="utf-8")
    prebundled = offline / "voice" / "olivia-reference.wav"
    prebundled.parent.mkdir()
    prebundled.write_bytes(reference.read_bytes())
    with pytest.raises(SetupBuildError, match="SETUP_INPUT_VOICE_REFERENCE_FORBIDDEN"):
        prepare_setup_payload(source, offline, tmp_path / "prebundled", validate_schema=False)
    for field in ("distribution", "voice_reference", "video_runtime"):
        del input_manifest[field]
    input_manifest_path.write_text(json.dumps(input_manifest), encoding="utf-8")
    with pytest.raises(SetupBuildError, match="SETUP_INPUT_VOICE_REFERENCE_FORBIDDEN"):
        prepare_setup_payload(
            source, offline, tmp_path / "prebundled-video-offline", validate_schema=False
        )
    del (
        input_manifest["video_offline"],
    )
    input_manifest_path.write_text(json.dumps(input_manifest), encoding="utf-8")
    with pytest.raises(SetupBuildError, match="SETUP_OFFLINE_ASSET_SET_MISMATCH"):
        prepare_setup_payload(source, offline, tmp_path / "orphan", validate_schema=False)
    with pytest.raises(SetupBuildError, match="SETUP_VOICE_REFERENCE_PRIVATE_ONLY"):
        prepare_setup_payload(
            source, offline, tmp_path / "public", voice_reference=reference, validate_schema=False
        )
    with pytest.raises(SetupBuildError, match="SETUP_PRIVATE_VIDEO_RUNTIME_REQUIRED"):
        prepare_setup_payload(
            source, offline, tmp_path / "private", distribution="private",
            voice_reference=reference, validate_schema=False
        )
    with pytest.raises(SetupBuildError, match="SETUP_VIDEO_RUNTIME_PRIVATE_ONLY"):
        prepare_setup_payload(
            source, offline, tmp_path / "public-runtime", video_runtime=runtime,
            validate_schema=False,
        )


def test_prepare_private_payload_requires_video_activation_entrypoint(
    tmp_path: Path, monkeypatch
) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "distributor/Olivia-video-runtime-fixture.zip"
    _write_video_runtime(runtime)
    monkeypatch.setattr(
        "installer.build_windows_setup._git_tracked_files",
        lambda _source: {
            "installer/Install.ps1",
            "installer/runtime-requirements.txt",
            "installer/video-capability-manifest.json",
            *BUILD_CONTROL_FILES,
        },
    )

    with pytest.raises(SetupBuildError, match="SETUP_REQUIRED_PAYLOAD_MISSING"):
        _prepare_private_runtime(
            source, offline, reference, runtime, tmp_path / "missing-activation"
        )


def test_prepare_private_payload_records_sidecar_without_copying_large_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "distributor" / "Olivia-video-runtime-v2.zip"
    _write_video_runtime_v2(runtime)
    destination = tmp_path / "payload"

    _prepare_private_runtime(source, offline, reference, runtime, destination)

    manifest = json.loads(
        (destination / "offline/offline-core-assets.json").read_text(encoding="utf-8")
    )
    assert manifest["video_runtime"] == {
        "path": "Olivia-video-runtime-private.zip",
        "size_bytes": runtime.stat().st_size,
        "sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
    }
    assert not (destination / "offline/video-runtime").exists()


def test_prepare_private_payload_pins_exact_shared_video_offline_root(
    tmp_path: Path, monkeypatch
) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "distributor/Olivia-video-runtime-v2.zip"
    _write_video_runtime_v2(runtime)
    video_offline = tmp_path / "distributor/Olivia-video-offline-fixture"
    destination = tmp_path / "payload"

    _prepare_private_runtime(source, offline, reference, runtime, destination)

    private_manifest = json.loads(
        (destination / "offline/offline-core-assets.json").read_text(
            encoding="utf-8"
        )
    )
    video_manifest = source / "installer/video-capability-manifest.json"
    assert private_manifest["video_offline"] == {
        "path": "Olivia-video-offline-private",
        "manifest_version": "fixture-video",
        "manifest_sha256": hashlib.sha256(video_manifest.read_bytes()).hexdigest(),
        "file_count": 2,
        "size_bytes": len(b"ordinary") + len(b"music"),
    }
    assert not (destination / "offline/video-offline").exists()

    (video_offline / "ordinary_video/ordinary.bin").write_bytes(b"tampered")
    with pytest.raises(SetupBuildError, match="SETUP_VIDEO_OFFLINE_INVALID"):
        prepare_setup_payload(
            source,
            offline,
            tmp_path / "tampered-payload",
            distribution="private",
            voice_reference=reference,
            video_runtime=runtime,
            video_offline_root=video_offline,
            validate_schema=False,
        )


def test_prepare_setup_payload_accepts_builder_v2_runtime_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "distributor" / "Olivia-video-runtime-v2.zip"
    _write_video_runtime_v2(runtime)

    _prepare_private_runtime(source, offline, reference, runtime, tmp_path / "payload")

    manifest = json.loads(
        (tmp_path / "payload/offline/offline-core-assets.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["video_runtime"]["path"] == "Olivia-video-runtime-private.zip"
    assert manifest["video_runtime"]["size_bytes"] == runtime.stat().st_size
    assert not (tmp_path / "payload/offline/video-runtime").exists()


@pytest.mark.parametrize(
    "case",
    [
        "version",
        "manifest-order",
        "build-input-shape",
        "missing-component",
        "upstream-malformed",
        "revision",
        "dependencies",
        "tree-hash",
        "legal-hash",
        "source-hash",
        "environment-not-python",
    ],
)
def test_prepare_setup_payload_rejects_invalid_v2_build_inputs(
    tmp_path: Path, monkeypatch, case: str
) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "Olivia-video-runtime-v2.zip"
    _write_video_runtime_v2(runtime)

    def mutate(manifest: dict[str, object]) -> None:
        build_inputs = manifest["build_inputs"]
        components = build_inputs["components"]
        cosyvoice = components["cosyvoice"]
        if case == "version":
            manifest["version"] = "not a builder version"
        elif case == "manifest-order":
            manifest["files"].reverse()
        elif case == "build-input-shape":
            build_inputs["extra"] = True
        elif case == "missing-component":
            components.pop("roformer")
        elif case == "upstream-malformed":
            cosyvoice["upstream"] = "https://["
        elif case == "revision":
            cosyvoice["revision"] = "A" * 40
        elif case == "dependencies":
            cosyvoice["dependencies"] = ["Torch==1", "torch==1"]
        elif case == "tree-hash":
            cosyvoice["tree_sha256"] = "0" * 64
        elif case == "legal-hash":
            cosyvoice["license"]["sha256"] = "0" * 64
        elif case == "source-hash":
            cosyvoice["files"][0]["sha256"] = "0" * 64
        elif case == "environment-not-python":
            manifest["environment"]["OLIVIA_COSYVOICE_PYTHON"] = (
                "cosyvoice/runtime/LICENSE.txt"
            )

    _mutate_video_runtime_manifest(runtime, mutate)

    with pytest.raises(SetupBuildError, match="SETUP_VIDEO_RUNTIME_INVALID"):
        _prepare_private_runtime(source, offline, reference, runtime, tmp_path / "payload")


@pytest.mark.parametrize(
    ("relative", "content", "dedicated_license"),
    [
        ("models/config.json", b"{}", True),
        ("weights.pt", b"private model", True),
        ("recording.wav", b"private media", True),
        ("recording.dat", b"RIFF" + b"\0" * 4 + b"WAVE", True),
        ("payload.bin", b"private model", True),
        ("site-packages/payload.pth", b"\x80binary model", True),
        (None, b"", False),
    ],
)
def test_prepare_setup_payload_rejects_v2_builder_forbidden_content(
    tmp_path: Path,
    monkeypatch,
    relative: str | None,
    content: bytes,
    dedicated_license: bool,
) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "Olivia-video-runtime-v2.zip"
    _write_video_runtime_v2(
        runtime,
        cosyvoice_extra=None if relative is None else {relative: content},
        dedicated_license=dedicated_license,
    )

    with pytest.raises(SetupBuildError, match="SETUP_VIDEO_RUNTIME_INVALID"):
        _prepare_private_runtime(source, offline, reference, runtime, tmp_path / "payload")


@pytest.mark.parametrize(
    ("limit_name", "limit"),
    [
        ("VIDEO_RUNTIME_MAX_ARCHIVE_BYTES", 1),
        ("VIDEO_RUNTIME_MAX_ENTRIES", 1),
        ("VIDEO_RUNTIME_MAX_MANIFEST_BYTES", 1),
        ("VIDEO_RUNTIME_MAX_EXPANDED_BYTES", 1),
        ("VIDEO_RUNTIME_MAX_COMPRESSION_RATIO", 1),
    ],
)
def test_prepare_setup_payload_applies_v2_zip_bounds_before_expansion(
    tmp_path: Path, monkeypatch, limit_name: str, limit: int
) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "Olivia-video-runtime-v2.zip"
    _write_video_runtime_v2(runtime)
    monkeypatch.setattr(f"installer.build_windows_setup.{limit_name}", limit)

    with pytest.raises(SetupBuildError, match="SETUP_VIDEO_RUNTIME_INVALID"):
        _prepare_private_runtime(source, offline, reference, runtime, tmp_path / "payload")


def test_prepare_setup_payload_accepts_realistic_high_ratio_v2_entry(
    tmp_path: Path, monkeypatch
) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "Olivia-video-runtime-v2.zip"
    payload_size = 1024 * 1024
    compressed_payload = (
        b"0" * (payload_size - 1950) + random.Random(17).randbytes(1950)
    )
    ballast = random.Random(23).randbytes(64 * 1024)
    _write_video_runtime_v2(
        runtime,
        cosyvoice_extra={
            "zz-large-data.txt": compressed_payload,
            "zz-ballast.txt": ballast,
        },
    )

    with zipfile.ZipFile(runtime) as archive:
        entry = archive.getinfo("cosyvoice/runtime/zz-large-data.txt")
        ratio = entry.file_size / entry.compress_size
        overall_ratio = sum(item.file_size for item in archive.infolist()) / runtime.stat().st_size
    assert 320 < ratio < 322
    assert overall_ratio < 200

    _prepare_private_runtime(source, offline, reference, runtime, tmp_path / "payload")


def test_prepare_setup_payload_rejects_v2_entry_above_ratio_limit(
    tmp_path: Path, monkeypatch
) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "Olivia-video-runtime-v2.zip"
    ballast = random.Random(23).randbytes(64 * 1024)
    _write_video_runtime_v2(
        runtime,
        cosyvoice_extra={
            "zz-large-data.txt": b"0" * (1024 * 1024),
            "zz-ballast.txt": ballast,
        },
    )

    with zipfile.ZipFile(runtime) as archive:
        entry = archive.getinfo("cosyvoice/runtime/zz-large-data.txt")
        ratio = entry.file_size / entry.compress_size
        overall_ratio = sum(item.file_size for item in archive.infolist()) / runtime.stat().st_size
    assert ratio > 512
    assert overall_ratio < 200

    with pytest.raises(SetupBuildError, match="SETUP_VIDEO_RUNTIME_INVALID"):
        _prepare_private_runtime(source, offline, reference, runtime, tmp_path / "payload")


def test_prepare_setup_payload_maps_unsupported_v2_zip_compression_to_contract(
    tmp_path: Path, monkeypatch
) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "Olivia-video-runtime-v2.zip"
    _write_video_runtime_v2(runtime)
    monkeypatch.setattr(
        zipfile.ZipFile, "read", lambda *_args, **_kwargs: (_ for _ in ()).throw(
            NotImplementedError("unsupported compression")
        )
    )

    with pytest.raises(SetupBuildError, match="SETUP_VIDEO_RUNTIME_INVALID"):
        _prepare_private_runtime(source, offline, reference, runtime, tmp_path / "payload")


def test_video_runtime_v2_fixture_matches_machine_contracts(tmp_path: Path) -> None:
    import jsonschema

    runtime = tmp_path / "Olivia-video-runtime-v2.zip"
    _write_video_runtime_v2(runtime)
    with zipfile.ZipFile(runtime) as archive:
        manifest = json.loads(archive.read("runtime-manifest.json"))
    build_inputs_schema = json.loads((ROOT / "contracts/video_runtime_build_inputs.schema.json").read_text())
    runtime_schema = json.loads((ROOT / "contracts/video_runtime_root_v2.schema.json").read_text())

    jsonschema.validate(manifest["build_inputs"], build_inputs_schema)
    jsonschema.validate(manifest, runtime_schema)
    assert set(build_inputs_schema["properties"]["components"]["required"]) == {"cosyvoice", "latentsync", "minimax", "roformer"}


def test_setup_build_cli_forwards_distributor_voice_reference(tmp_path: Path, monkeypatch) -> None:
    reference = tmp_path / "olivia-reference.wav"
    reference.write_bytes(b"RIFF-voice")
    runtime = tmp_path / "Olivia-video-runtime-fixture.zip"
    runtime.write_bytes(b"runtime")
    video_offline = tmp_path / "Olivia-video-offline-private"
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "installer.build_windows_setup.build_windows_setup",
        lambda *args, **kwargs: captured.update(kwargs) or {"status": "OK"},
    )

    result = build_setup_main([
        "--offline", str(tmp_path / "offline"), "--output", str(tmp_path / "output"),
        "--version", "0.1.test", "--distribution", "private", "--voice-reference", str(reference),
        "--video-runtime", str(runtime), "--video-offline-root", str(video_offline),
    ])

    assert result == 0
    assert captured["distribution"] == "private"
    assert captured["voice_reference"] == reference
    assert captured["video_runtime"] == runtime
    assert captured["video_offline_root"] == video_offline


def test_failed_private_setup_compile_removes_partial_final_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "video-runtime.zip"
    _write_video_runtime(runtime)
    video_offline = reference.parent / "Olivia-video-offline-fixture"
    output = tmp_path / "dist-private"
    compiler = tmp_path / "ISCC.exe"
    compiler.write_bytes(b"compiler")
    contracts = source / "contracts"
    contracts.mkdir()
    (contracts / "offline_core_assets.schema.json").write_text(
        json.dumps({"type": "object"}),
        encoding="utf-8",
    )

    def fail_compile(_command, *, check, timeout):
        assert check is False
        assert timeout == 900
        (output / "Olivia-Setup-x64.exe").write_bytes(b"partial private setup")
        (output / "Olivia-Setup-x64-1.bin").write_bytes(b"partial runtime")
        return type("Result", (), {"returncode": 1})()

    monkeypatch.setattr("installer.build_windows_setup.subprocess.run", fail_compile)

    with pytest.raises(SetupBuildError, match="SETUP_COMPILE_FAILED"):
        build_windows_setup(
            source,
            offline,
            output,
            version="0.1.test",
            iscc=compiler,
            distribution="private",
            voice_reference=reference,
            video_runtime=runtime,
            video_offline_root=video_offline,
        )

    assert not (output / "Olivia-Setup-x64.exe").exists()
    assert not (output / "Olivia-Setup-x64-1.bin").exists()
    assert not (output / "Olivia-Setup-x64.exe.sha256").exists()


def test_failed_private_setup_checksum_removes_setup_and_partial_checksum(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "video-runtime.zip"
    _write_video_runtime(runtime)
    video_offline = reference.parent / "Olivia-video-offline-fixture"
    output = tmp_path / "dist-private"
    compiler = tmp_path / "ISCC.exe"
    compiler.write_bytes(b"compiler")
    contracts = source / "contracts"
    contracts.mkdir()
    (contracts / "offline_core_assets.schema.json").write_text(
        json.dumps({"type": "object"}),
        encoding="utf-8",
    )

    def compile_setup(_command, *, check, timeout):
        assert check is False
        assert timeout == 900
        (output / "Olivia-Setup-x64.exe").write_bytes(b"complete private setup")
        return type("Result", (), {"returncode": 0})()

    original_write_text = Path.write_text

    def fail_checksum_write(path: Path, content: str, *args, **kwargs):
        if path.name == "Olivia-Setup-x64.exe.sha256":
            original_write_text(path, "partial checksum", encoding="ascii")
            raise OSError("checksum write failed")
        return original_write_text(path, content, *args, **kwargs)

    monkeypatch.setattr("installer.build_windows_setup.subprocess.run", compile_setup)
    monkeypatch.setattr(Path, "write_text", fail_checksum_write)

    with pytest.raises(SetupBuildError, match="SETUP_BUILD_FAILED"):
        build_windows_setup(
            source,
            offline,
            output,
            version="0.1.test",
            iscc=compiler,
            distribution="private",
            voice_reference=reference,
            video_runtime=runtime,
            video_offline_root=video_offline,
        )

    assert not (output / "Olivia-Setup-x64.exe").exists()
    assert not (output / "Olivia-video-runtime-private.zip").exists()
    assert not (output / "Olivia-video-offline-private").exists()
    assert not (output / "Olivia-Setup-x64.receipt.json").exists()
    assert not (output / "Olivia-Setup-x64.exe.sha256").exists()


def test_windows_setup_docs_separate_public_and_private_voice_artifacts() -> None:
    documentation = (ROOT / "docs" / "WINDOWS_FULL_PATCH.md").read_text(
        encoding="utf-8"
    )

    assert "公开安装器不包含参考音频" in documentation
    assert "--distribution private" in documentation
    assert "--voice-reference" in documentation
    assert "--video-runtime" in documentation
    assert "--video-offline-root" in documentation
    assert "dist-private" in documentation
    assert "Olivia-video-runtime-private.zip" in documentation
    assert "Olivia-video-offline-private" in documentation
    assert "Olivia-Setup-x64.receipt.json" in documentation
    assert "不会进入 Inno `{tmp}`" in documentation
    assert "私有视频包：下载完整 ZIP" in documentation
    assert "除私有模式显式传入的 WAV、视频运行时 ZIP 与视频离线根目录外" in documentation
    assert "ordinary_video" in documentation
    assert "music_video" in documentation
    assert "因而没有自引用" in documentation
    assert "GitHub Actions 只生成公开安装器 artifact" in documentation


def test_prepare_setup_payload_rejects_truncated_voice_reference(tmp_path: Path, monkeypatch) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "Olivia-video-runtime-fixture.zip"
    _write_video_runtime(runtime)
    reference.write_bytes(reference.read_bytes()[:-1])

    with pytest.raises(SetupBuildError, match="SETUP_VOICE_REFERENCE_TRUNCATED"):
        prepare_setup_payload(
            source,
            offline,
            tmp_path / "payload",
            distribution="private",
            voice_reference=reference,
            video_runtime=runtime,
            video_offline_root=reference.parent / "Olivia-video-offline-fixture",
            validate_schema=False,
        )


def test_private_inno_payload_reads_large_runtime_beside_setup_without_tmp_extract() -> None:
    script = (ROOT / "installer/windows_setup.iss").read_text(encoding="utf-8")
    assert "#ifdef PrivatePayload" in script
    assert "DiskSpanning=yes" not in script
    assert "DiskSliceSize=" not in script
    assert 'Source: "{#PayloadRoot}\\offline\\video-runtime\\*"' not in script
    assert "-VideoRuntimePath" in script
    assert "{src}\\Olivia-video-runtime-private.zip" in script
    assert "-VideoOfflineRoot" in script
    assert "{src}\\Olivia-video-offline-private" in script


def test_private_build_publishes_hash_locked_video_runtime_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "output"
    observed: list[str] = []
    runtime = tmp_path / "runtime.zip"
    runtime.write_bytes(b"runtime")
    video_offline = tmp_path / "video-offline"
    (video_offline / "ordinary_video").mkdir(parents=True)
    (video_offline / "music_video").mkdir()
    (video_offline / "ordinary_video" / "ordinary.bin").write_bytes(b"ordinary")
    (video_offline / "music_video" / "music.bin").write_bytes(b"music")

    def compile_setup(command: list[str], **_: object) -> SimpleNamespace:
        observed.extend(command)
        (output / "Olivia-Setup-x64.exe").write_bytes(b"setup")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("installer.build_windows_setup._find_iscc", lambda _: tmp_path / "ISCC.exe")
    monkeypatch.setattr("installer.build_windows_setup.prepare_setup_payload", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "installer.build_windows_setup._verify_pinned_private_sidecars",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("installer.build_windows_setup.subprocess.run", compile_setup)

    result = build_windows_setup(
        tmp_path / "source",
        tmp_path / "offline",
        output,
        version="fixture",
        distribution="private",
        voice_reference=tmp_path / "voice.wav",
        video_runtime=runtime,
        video_offline_root=video_offline,
    )

    assert "/DPrivatePayload=1" in observed
    assert [Path(item["path"]).name for item in result["artifacts"]] == [
        "Olivia-Setup-x64.exe",
        "Olivia-video-runtime-private.zip",
        "Olivia-video-offline-private",
    ]
    assert (output / "Olivia-video-runtime-private.zip").read_bytes() == b"runtime"
    assert (
        output / "Olivia-video-offline-private/ordinary_video/ordinary.bin"
    ).read_bytes() == b"ordinary"
    assert (
        output / "Olivia-video-offline-private/music_video/music.bin"
    ).read_bytes() == b"music"
    assert not list(output.glob("Olivia-Setup-x64-*.bin"))
    checksum = (output / "Olivia-Setup-x64.exe.sha256").read_text(encoding="ascii")
    assert "Olivia-Setup-x64.exe" in checksum
    assert "Olivia-video-runtime-private.zip" in checksum
    assert "Olivia-video-offline-private/ordinary_video/ordinary.bin" in checksum
    assert "Olivia-video-offline-private/music_video/music.bin" in checksum
    receipt = json.loads(
        (output / "Olivia-Setup-x64.receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["schema_version"] == "olivia.private-setup-receipt.v1"
    assert receipt["distribution"] == "private"
    assert receipt["offline_root"] == "Olivia-video-offline-private"
    receipt_paths = [item["path"] for item in receipt["files"]]
    assert receipt_paths == [
        "Olivia-Setup-x64.exe",
        "Olivia-video-runtime-private.zip",
        "Olivia-video-offline-private/music_video/music.bin",
        "Olivia-video-offline-private/ordinary_video/ordinary.bin",
    ]
    assert all(not Path(path).is_absolute() for path in receipt_paths)
    assert set(path.name for path in output.iterdir()) == {
        "Olivia-Setup-x64.exe",
        "Olivia-video-runtime-private.zip",
        "Olivia-video-offline-private",
        "Olivia-Setup-x64.receipt.json",
        "Olivia-Setup-x64.exe.sha256",
    }

    checksum_records = {}
    for line in checksum.splitlines():
        digest, relative = line.split("  ", 1)
        checksum_records[relative] = digest
    assert set(checksum_records) == {*receipt_paths, "Olivia-Setup-x64.receipt.json"}
    for relative, digest in checksum_records.items():
        assert hashlib.sha256((output / relative).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize(
    ("marked_source", "error_code"),
    [
        ("runtime", "SETUP_VIDEO_RUNTIME_INVALID"),
        ("offline-file", "SETUP_VIDEO_OFFLINE_INVALID"),
        ("offline-output", "SETUP_VIDEO_OFFLINE_INVALID"),
    ],
)
def test_private_build_rejects_reparse_sidecar_sources_before_copy(
    tmp_path: Path,
    monkeypatch,
    marked_source: str,
    error_code: str,
) -> None:
    output = tmp_path / "output"
    runtime = tmp_path / "runtime.zip"
    runtime.write_bytes(b"runtime")
    video_offline = tmp_path / "video-offline"
    (video_offline / "ordinary_video").mkdir(parents=True)
    (video_offline / "music_video").mkdir()
    ordinary = video_offline / "ordinary_video/ordinary.bin"
    music = video_offline / "music_video/music.bin"
    ordinary.write_bytes(b"ordinary")
    music.write_bytes(b"music")
    marked = {
        "runtime": runtime,
        "offline-file": music,
        "offline-output": (
            output
            / "Olivia-video-offline-private/music_video/music.bin"
        ),
    }[marked_source]

    def compile_setup(_command: list[str], **_: object) -> SimpleNamespace:
        (output / "Olivia-Setup-x64.exe").write_bytes(b"setup")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(
        "installer.build_windows_setup._find_iscc", lambda _: tmp_path / "ISCC.exe"
    )
    monkeypatch.setattr(
        "installer.build_windows_setup.prepare_setup_payload", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "installer.build_windows_setup._verify_pinned_private_sidecars",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "installer.build_windows_setup._is_reparse_point",
        lambda path: Path(path) == marked,
    )
    monkeypatch.setattr("installer.build_windows_setup.subprocess.run", compile_setup)

    with pytest.raises(SetupBuildError, match=error_code):
        build_windows_setup(
            tmp_path / "source",
            tmp_path / "offline",
            output,
            version="fixture",
            distribution="private",
            voice_reference=tmp_path / "voice.wav",
            video_runtime=runtime,
            video_offline_root=video_offline,
        )


def test_private_build_rejects_runtime_sidecar_changed_after_payload_pin(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "output"
    runtime = tmp_path / "runtime.zip"
    runtime.write_bytes(b"runtime")
    video_offline = tmp_path / "video-offline"
    (video_offline / "ordinary_video").mkdir(parents=True)
    (video_offline / "music_video").mkdir()
    ordinary = video_offline / "ordinary_video/ordinary.bin"
    music = video_offline / "music_video/music.bin"
    ordinary.write_bytes(b"ordinary")
    music.write_bytes(b"music")

    def pin_payload(*args: object, **kwargs: object) -> None:
        payload = Path(args[2])
        (payload / "offline").mkdir(parents=True)
        (payload / "installer").mkdir()
        runtime_sidecar = Path(kwargs["video_runtime"])
        offline_sidecar = Path(kwargs["video_offline_root"])
        video_manifest = {
            "schema_version": "olivia.video-capability-bom.v1",
            "version": "fixture-video",
            "bundles": [
                {
                    "id": bundle,
                    "files": [
                        {
                            "path": path.name,
                            "size_bytes": path.stat().st_size,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        }
                    ],
                }
                for bundle, path in (("ordinary_video", ordinary), ("music_video", music))
            ],
        }
        video_manifest_bytes = json.dumps(video_manifest).encode()
        (payload / "installer/video-capability-manifest.json").write_bytes(
            video_manifest_bytes
        )
        (payload / "offline/offline-core-assets.json").write_text(
            json.dumps(
                {
                    "video_runtime": {
                        "path": "Olivia-video-runtime-private.zip",
                        "size_bytes": runtime_sidecar.stat().st_size,
                        "sha256": hashlib.sha256(runtime_sidecar.read_bytes()).hexdigest(),
                    },
                    "video_offline": {
                        "path": "Olivia-video-offline-private",
                        "manifest_version": "fixture-video",
                        "manifest_sha256": hashlib.sha256(video_manifest_bytes).hexdigest(),
                        "file_count": 2,
                        "size_bytes": sum(
                            path.stat().st_size
                            for path in offline_sidecar.rglob("*")
                            if path.is_file()
                        ),
                    },
                }
            ),
            encoding="utf-8",
        )

    def compile_setup(_command: list[str], **_: object) -> SimpleNamespace:
        (output / "Olivia-Setup-x64.exe").write_bytes(b"setup")
        (output / "Olivia-video-runtime-private.zip").write_bytes(b"changed")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("installer.build_windows_setup._find_iscc", lambda _: tmp_path / "ISCC.exe")
    monkeypatch.setattr("installer.build_windows_setup.prepare_setup_payload", pin_payload)
    monkeypatch.setattr("installer.build_windows_setup.subprocess.run", compile_setup)

    with pytest.raises(SetupBuildError, match="SETUP_PRIVATE_SIDECAR_CHANGED"):
        build_windows_setup(
            tmp_path / "source",
            tmp_path / "offline",
            output,
            version="fixture",
            distribution="private",
            voice_reference=tmp_path / "voice.wav",
            video_runtime=runtime,
            video_offline_root=video_offline,
        )

    assert not any(output.iterdir())


def test_private_build_receipt_reuses_post_compile_verified_sidecar_records(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "distributor/Olivia-video-runtime-fixture.zip"
    _write_video_runtime(runtime)
    expected_runtime_sha256 = hashlib.sha256(runtime.read_bytes()).hexdigest()
    video_offline = reference.parent / "Olivia-video-offline-fixture"
    expected_ordinary_sha256 = hashlib.sha256(b"ordinary").hexdigest()
    output = tmp_path / "output"

    def prepare_without_schema(*args: object, **kwargs: object) -> None:
        kwargs["validate_schema"] = False
        prepare_setup_payload(*args, **kwargs)

    def compile_setup(_command: list[str], **_: object) -> SimpleNamespace:
        (output / "Olivia-Setup-x64.exe").write_bytes(b"setup")
        return SimpleNamespace(returncode=0)

    original_verify = setup_builder._verify_pinned_private_sidecars

    def verify_then_replace(
        payload: Path,
        runtime_sidecar: Path,
        offline_sidecar: Path,
    ) -> object:
        verified = original_verify(payload, runtime_sidecar, offline_sidecar)
        runtime_sidecar.write_bytes(b"replacement runtime")
        (offline_sidecar / "ordinary_video/ordinary.bin").write_bytes(b"replaced")
        return verified

    monkeypatch.setattr(
        "installer.build_windows_setup._find_iscc", lambda _: tmp_path / "ISCC.exe"
    )
    monkeypatch.setattr(
        "installer.build_windows_setup.prepare_setup_payload", prepare_without_schema
    )
    monkeypatch.setattr(
        "installer.build_windows_setup._verify_pinned_private_sidecars",
        verify_then_replace,
    )
    monkeypatch.setattr("installer.build_windows_setup.subprocess.run", compile_setup)

    build_windows_setup(
        source,
        offline,
        output,
        version="fixture",
        distribution="private",
        voice_reference=reference,
        video_runtime=runtime,
        video_offline_root=video_offline,
    )

    receipt = json.loads(
        (output / "Olivia-Setup-x64.receipt.json").read_text(encoding="utf-8")
    )
    receipt_records = {record["path"]: record for record in receipt["files"]}
    checksum_records = {
        relative: digest
        for digest, relative in (
            line.split("  ", 1)
            for line in (output / "Olivia-Setup-x64.exe.sha256")
            .read_text(encoding="ascii")
            .splitlines()
        )
    }

    assert (output / "Olivia-video-runtime-private.zip").read_bytes() == b"replacement runtime"
    assert (
        output / "Olivia-video-offline-private/ordinary_video/ordinary.bin"
    ).read_bytes() == b"replaced"
    assert receipt_records["Olivia-video-runtime-private.zip"]["sha256"] == (
        expected_runtime_sha256
    )
    assert receipt_records[
        "Olivia-video-offline-private/ordinary_video/ordinary.bin"
    ]["sha256"] == expected_ordinary_sha256
    assert checksum_records["Olivia-video-runtime-private.zip"] == (
        expected_runtime_sha256
    )
    assert checksum_records[
        "Olivia-video-offline-private/ordinary_video/ordinary.bin"
    ] == expected_ordinary_sha256


@pytest.mark.parametrize("parts", [(1,), (1, 3)])
def test_private_build_rejects_unexpected_compiler_disk_parts(
    tmp_path: Path, monkeypatch, parts: tuple[int, ...]
) -> None:
    output = tmp_path / "output"
    runtime = tmp_path / "runtime.zip"
    runtime.write_bytes(b"runtime")
    video_offline = tmp_path / "video-offline"
    video_offline.mkdir()

    def compile_setup(_command: list[str], **_: object) -> SimpleNamespace:
        (output / "Olivia-Setup-x64.exe").write_bytes(b"setup")
        for part in parts:
            (output / f"Olivia-Setup-x64-{part}.bin").write_bytes(b"runtime")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("installer.build_windows_setup._find_iscc", lambda _: tmp_path / "ISCC.exe")
    monkeypatch.setattr("installer.build_windows_setup.prepare_setup_payload", lambda *args, **kwargs: None)
    monkeypatch.setattr("installer.build_windows_setup.subprocess.run", compile_setup)

    with pytest.raises(SetupBuildError, match="SETUP_COMPILE_FAILED"):
        build_windows_setup(
            tmp_path / "source", tmp_path / "offline", output, version="fixture",
            distribution="private", voice_reference=tmp_path / "voice.wav",
            video_runtime=runtime,
            video_offline_root=video_offline,
        )

    assert not list(output.glob("Olivia-Setup-x64*"))


@pytest.mark.parametrize("case", [
    "missing-version", "missing-python", "duplicate-python", "traversal", "backslash",
    "duplicate", "case-alias", "undeclared", "self-reference", "bad-hash", "file-shape",
])
def test_prepare_setup_payload_rejects_invalid_runtime_zip(
    tmp_path: Path, monkeypatch, case: str
) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "runtime.zip"
    _write_video_runtime(runtime)

    def mutate(manifest: dict[str, object]) -> None:
        fixture = next(item for item in manifest["files"] if item["path"] == "fixture.bin")
        if case == "missing-version":
            manifest.pop("version")
        elif case == "missing-python":
            manifest["environment"].pop("OLIVIA_ROFORMER_PYTHON")
        elif case == "duplicate-python":
            manifest["environment"] = dict.fromkeys(
                manifest["environment"], "cosyvoice/python/python.exe"
            )
        elif case in {"traversal", "backslash"}:
            fixture["path"] = "../escape.bin" if case == "traversal" else "dir\\escape.bin"
        elif case == "self-reference":
            fixture["path"] = "runtime-manifest.json"
        elif case == "bad-hash":
            fixture["sha256"] = "0" * 64
        elif case == "file-shape":
            fixture["extra"] = True

    _mutate_video_runtime_manifest(runtime, mutate)
    extras = {"duplicate": "fixture.bin", "case-alias": "FIXTURE.BIN", "undeclared": "extra.bin"}
    if case in extras:
        with zipfile.ZipFile(runtime, "a") as archive:
            archive.writestr(extras[case], b"x")

    with pytest.raises(SetupBuildError, match="SETUP_VIDEO_RUNTIME_INVALID"):
        prepare_setup_payload(
            source, offline, tmp_path / "payload", distribution="private",
            voice_reference=reference, video_runtime=runtime,
            video_offline_root=reference.parent / "Olivia-video-offline-fixture",
            validate_schema=False,
        )


@pytest.mark.parametrize("unsafe", ["NUL/file.bin", "bad:name.bin", "trailing./file.bin"])
def test_video_runtime_paths_follow_windows_rules(unsafe: str) -> None:
    with pytest.raises(SetupBuildError, match="SETUP_VIDEO_RUNTIME_INVALID"):
        _video_runtime_relative(unsafe)


@pytest.mark.parametrize(
    "suffix",
    [
        ".aac",
        ".avi",
        ".flac",
        ".m4a",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".ogg",
        ".opus",
        ".wav",
        ".webm",
        ".wmv",
    ],
)
@pytest.mark.parametrize("distribution", ["public", "private"])
def test_setup_rejects_git_selected_audio_and_video_payloads(
    tmp_path: Path,
    monkeypatch,
    suffix: str,
    distribution: str,
) -> None:
    source, offline, reference = _voice_setup_fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "video-runtime.zip"
    _write_video_runtime(runtime)
    relative = f"runtime/reference{suffix}"
    media = source.joinpath(*relative.split("/"))
    media.parent.mkdir()
    media.write_bytes(b"private media")
    monkeypatch.setattr(
        "installer.build_windows_setup._git_tracked_files",
            lambda _source: {
                "installer/Install.ps1",
                "installer/activate_private_video.py",
                "installer/runtime-requirements.txt",
                *BUILD_CONTROL_FILES,
                relative,
        },
    )

    with pytest.raises(SetupBuildError, match="SETUP_TRACKED_MEDIA_FORBIDDEN"):
        prepare_setup_payload(
            source,
            offline,
            tmp_path / "payload",
            distribution=distribution,
            voice_reference=reference if distribution == "private" else None,
            video_runtime=runtime if distribution == "private" else None,
            video_offline_root=(
                reference.parent / "Olivia-video-offline-fixture"
                if distribution == "private"
                else None
            ),
            validate_schema=False,
        )


def test_prepare_setup_payload_rejects_dirty_tracked_release_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    offline = tmp_path / "offline"
    (source / "installer").mkdir(parents=True)
    requirements = b"locked requirements"
    (source / "installer" / "runtime-requirements.txt").write_bytes(requirements)
    (source / "installer" / "Install.ps1").write_text("install", encoding="utf-8")
    (source / "local_server.py").write_text("dirty", encoding="utf-8")
    _offline_fixture(offline, requirements)
    monkeypatch.setattr(
        "installer.build_windows_setup._git_tracked_files",
        lambda _source: {
            "installer/Install.ps1",
            "installer/runtime-requirements.txt",
            *BUILD_CONTROL_FILES,
            "local_server.py",
        },
    )
    monkeypatch.setattr(
        "installer.build_windows_setup._git_dirty_files",
        lambda _source: {"local_server.py"},
    )

    with pytest.raises(SetupBuildError, match="SETUP_SOURCE_DIRTY"):
        prepare_setup_payload(source, offline, tmp_path / "payload", validate_schema=False)


def test_setup_payload_uses_positive_runtime_allowlist() -> None:
    assert _is_release_file("local_server.py")
    assert _is_release_file("original_client_update_api.py")
    assert _is_release_file("control_center/static/index.html")
    assert _is_release_file("installer/full_patch.py")
    assert _is_release_file("installer/activate_private_video.py")
    assert _is_release_file("installer/component_package.py")
    assert _is_release_file("installer/seed-vc-overlap-frames.patch")
    assert _is_release_file("installer/start_hidden.vbs.txt")
    assert _is_release_file("installer/assets/olivia.ico")
    assert _is_release_file("tools/livetalking_worker.py")

    assert not _is_release_file(".gitignore")
    assert not _is_release_file("baseline_hardening_scan.py")
    assert not _is_release_file("installer/build_offline_core_assets.py")
    assert not _is_release_file("tools/verify_b11_scope.py")
    assert not _is_release_file("tools/live_e2e_acceptance.py")


def test_real_head_setup_payload_excludes_build_audit_test_and_scm_files() -> None:
    selected = {
        relative
        for relative in _git_tracked_files(ROOT)
        if _is_release_file(relative)
    }

    assert {
        "installer/Install.ps1",
        "installer/assets/olivia.ico",
        "installer/full_patch.py",
        "installer/component_package.py",
        "installer/seed-vc-overlap-frames.patch",
        "local_server.py",
        "control_center/static/index.html",
    }.issubset(selected)
    assert not any(
        relative.startswith((".", "docs/", "tests/"))
        or relative.startswith("tools/verify_")
        or "/audit_" in relative
        or "acceptance" in relative
        or relative.startswith("installer/build_")
        for relative in selected
    )


def test_prepare_setup_payload_rejects_dirty_setup_build_control_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    offline = tmp_path / "offline"
    (source / "installer").mkdir(parents=True)
    requirements = b"locked requirements"
    (source / "installer" / "runtime-requirements.txt").write_bytes(requirements)
    (source / "installer" / "Install.ps1").write_text("install", encoding="utf-8")
    _offline_fixture(offline, requirements)
    monkeypatch.setattr(
        "installer.build_windows_setup._git_tracked_files",
        lambda _source: {
            "installer/Install.ps1",
            "installer/runtime-requirements.txt",
            *BUILD_CONTROL_FILES,
        },
    )
    monkeypatch.setattr(
        "installer.build_windows_setup._git_dirty_files",
        lambda _source: {"installer/windows_setup.iss"},
    )

    with pytest.raises(SetupBuildError, match="SETUP_SOURCE_DIRTY"):
        prepare_setup_payload(source, offline, tmp_path / "payload", validate_schema=False)


def test_prepare_setup_payload_rejects_tampered_offline_assets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    offline = tmp_path / "offline"
    (source / "installer").mkdir(parents=True)
    requirements = b"locked requirements"
    (source / "installer" / "runtime-requirements.txt").write_bytes(requirements)
    (source / "installer" / "Install.ps1").write_text("install", encoding="utf-8")
    _offline_fixture(offline, requirements)
    (offline / "wheelhouse" / "core.whl").write_bytes(b"tampered")
    monkeypatch.setattr(
        "installer.build_windows_setup._git_tracked_files",
        lambda _source: {
            "installer/Install.ps1",
            "installer/runtime-requirements.txt",
            *BUILD_CONTROL_FILES,
        },
    )
    monkeypatch.setattr(
        "installer.build_windows_setup._git_dirty_files", lambda _source: set()
    )

    with pytest.raises(SetupBuildError, match="SETUP_OFFLINE_ASSET_HASH_MISMATCH"):
        prepare_setup_payload(source, offline, tmp_path / "payload", validate_schema=False)


def test_prepare_setup_payload_rejects_reparse_parent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    offline = tmp_path / "offline"
    (source / "installer").mkdir(parents=True)
    requirements = b"locked requirements"
    (source / "installer" / "runtime-requirements.txt").write_bytes(requirements)
    (source / "installer" / "Install.ps1").write_text("install", encoding="utf-8")
    _offline_fixture(offline, requirements)
    monkeypatch.setattr(
        "installer.build_windows_setup._git_tracked_files",
        lambda _source: {
            "installer/Install.ps1",
            "installer/runtime-requirements.txt",
        },
    )
    monkeypatch.setattr(
        "installer.build_windows_setup._git_dirty_files", lambda _source: set()
    )
    monkeypatch.setattr(
        "installer.build_windows_setup._is_reparse_point",
        lambda path: path.name == "wheelhouse",
    )

    with pytest.raises(SetupBuildError, match="SETUP_PATH_REPARSE_POINT"):
        prepare_setup_payload(source, offline, tmp_path / "payload", validate_schema=False)


def test_inno_wrapper_is_current_user_offline_and_delegates_to_install_ps1() -> None:
    script = (ROOT / "installer" / "windows_setup.iss").read_text(encoding="utf-8")

    assert "PrivilegesRequired=lowest" in script
    assert "CreateAppDir=no" in script
    assert "Uninstallable=no" in script
    assert "LicenseFile=" in script
    assert "SetupIconFile={#PayloadRoot}\\installer\\assets\\olivia.ico" in script
    assert "Install.ps1" in script
    assert "-NonInteractive" in script
    assert "GetInstallRoot" in script
    assert "Exec(" in script
    assert "ExecAndLogOutput" not in script
    assert "StableInstallCode" in script
    assert "SetupResultPath" in script
    assert "OLIVIA_SETUP_ERROR=" in script
    assert "OFFICIAL_INSTALL_AMBIGUOUS" in script
    assert "上一步" in script
    assert ".diagnostic.json" in script
    assert "Olivia installer diagnostic:" in script
    assert "function PrepareToInstall" in script
    assert "dontcopy noencryption" in script
    assert "OfficialDirPage: TInputQueryWizardPage" in script
    assert "BrowseForFolder" in script
    assert "{param:InstallRoot|" in script
    assert "{localappdata}\\BSideOliviaLocal\\install}" not in script
    assert "产品目录" in script
    assert "{param:OfficialRoot|" in script
    assert "API" not in script
    assert "Hugging Face" not in script


def test_inno_wrapper_creates_launch_shortcuts_and_offers_immediate_start() -> None:
    script = (ROOT / "installer" / "windows_setup.iss").read_text(encoding="utf-8")

    assert '[Tasks]' in script
    assert 'Name: "desktopicon"' in script
    assert '[Icons]' in script
    assert '{userprograms}\\Olivia 本地版' in script
    assert '{userdesktop}\\Olivia 本地版' in script
    assert '[Run]' in script
    assert 'Description: "立即启动 Olivia"' in script
    assert '{sys}\\wscript.exe' in script
    assert '\\install\\START.vbs' in script
    assert '\\install\\START.cmd' not in script
    hidden_launch = (
        'Filename: "{sys}\\wscript.exe"; Parameters: "//B //Nologo '
        '""{code:GetInstallRoot}\\install\\START.vbs"""'
    )
    assert script.count(hidden_launch) == 3
    assert script.count('WorkingDir: "{code:GetInstallRoot}\\install"') == 3
    assert "' -SkipShortcut'" in script
    assert 'IconFilename: "{code:GetInstallRoot}\\install\\local_backend\\installer\\assets\\olivia.ico"' in script


def test_install_ps1_supports_noninteractive_setup_without_optional_downloads() -> None:
    script = (ROOT / "installer" / "Install.ps1").read_text(encoding="utf-8-sig")

    assert "[switch]$NonInteractive" in script
    assert "[string]$SetupResultPath" in script
    assert "OLIVIA_SETUP_ERROR=" in script
    assert "SETUP_INSTALL_FAILED" in script
    assert "if (-not $selectedOfficial -and -not $NonInteractive)" in script
    assert "Invoke-WebRequest" not in script
    assert "provision_mem0_embedding.py" not in script


def test_windows_installer_documents_ambiguous_source_diagnostic_contract() -> None:
    documentation = (ROOT / "docs" / "WINDOWS_FULL_PATCH.md").read_text(
        encoding="utf-8"
    )

    assert "OFFICIAL_INSTALL_AMBIGUOUS" in documentation
    assert "olivia.setup-source-diagnostic.v1" in documentation
    assert "selected_official_id" in documentation
    assert "observed_feapp_sha256" in documentation
    assert "observed_webplayer_sha256" in documentation


def test_github_build_publishes_setup_and_checksum_for_merged_main() -> None:
    workflow = (ROOT / ".github" / "workflows" / "windows-setup.yml").read_text(
        encoding="utf-8"
    )

    assert "branches: [main]" in workflow
    assert "build_offline_core_assets.py" in workflow
    assert "build_windows_setup.py" in workflow
    assert "pip install --require-hashes" in workflow
    assert "installer/setup-build-requirements.txt" in workflow
    assert "choco install innosetup --version=6.7.1" in workflow
    assert "Get-AuthenticodeSignature" in workflow
    assert "Pyrsys B.V." in workflow
    assert "is-6_7_1/Files/Languages/Unofficial/ChineseSimplified.isl" in workflow
    assert "7d544b9bb1d142cfa11f2e5d3cc8abe2e55f8e066c5124e3772675aa236e1278" in workflow
    assert "Olivia-Setup-x64.exe.sha256" in workflow
    assert "windows_setup_smoke.ps1" in workflow
    assert "actions/upload-artifact@v4" in workflow


def test_setup_failure_smoke_copies_the_valid_repository_icon() -> None:
    smoke = (ROOT / "tests" / "installer" / "windows_setup_smoke.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert "installer\\assets\\olivia.ico" in smoke
    assert "Copy-Item -LiteralPath $sourceIcon -Destination $fixtureIcon" in smoke


def test_setup_build_requirements_are_exact_and_hash_locked() -> None:
    requirements = (
        ROOT / "installer" / "setup-build-requirements.txt"
    ).read_text(encoding="utf-8")
    entries = [line for line in requirements.splitlines() if line and not line.startswith("#")]

    assert any(line.startswith("jsonschema==") for line in entries)
    assert all(" --hash=sha256:" in line for line in entries)


def test_third_party_notices_cover_setup_compiler_and_chinese_messages() -> None:
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    assert "Inno Setup" in notices
    assert "ChineseSimplified.isl" in notices
    assert "is-6_7_1" in notices
    assert "7d544b9bb1d142cfa11f2e5d3cc8abe2e55f8e066c5124e3772675aa236e1278" in notices


def test_runtime_third_party_notices_cover_every_locked_offline_wheel() -> None:
    requirements = (ROOT / "installer" / "runtime-requirements.txt").read_text(
        encoding="utf-8"
    )
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    packages = {
        line.split("==", maxsplit=1)[0]
        for line in requirements.splitlines()
        if line and not line.startswith("#")
    }

    assert packages
    assert all(f"`{package}`" in notices for package in packages)
    assert "Python 3.12.10 embeddable package" in notices
    assert "`pip` 25.2" in notices


def test_first_release_notes_cover_user_facing_release_boundaries() -> None:
    notes = (ROOT / "docs" / "releases" / "v0.1.0.md").read_text(
        encoding="utf-8"
    )

    for heading in ("已验证能力", "需要用户准备", "已知限制", "升级与回滚"):
        assert f"## {heading}" in notes
    assert "Olivia-Setup-x64.exe" in notes
    assert "未进行 Authenticode" in notes
    assert "未知发布者" in notes
    assert "SHA-256" in notes


def test_readme_release_status_matches_deferred_optional_model_install() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Mem0 一键新装仍是发布阻断" not in readme
    assert "可选模型在登录后的初始设置中按需安装" in readme
    assert "当前状态：发布候选" not in readme
    assert "发布边界" in readme
