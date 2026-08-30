from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import wave
import zipfile

from jsonschema import Draft202012Validator
import pytest


ROOT = Path(__file__).resolve().parents[2]


def _voice_reference_bytes() -> bytes:
    payload = io.BytesIO()
    with wave.open(payload, "wb") as target:
        target.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        target.writeframes(b"\x00\x00" * 160)
    return payload.getvalue()


def _managed_reference(product: Path) -> Path:
    return product / "install/data/capabilities/video/shared/linli-reference.wav"


def _wave_metadata(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {"channels": 1, "sample_width_bytes": 2, "sample_rate_hz": 16000, "frame_count": 160, "compression_type": "NONE"}
    value.update(changes)
    return value


def _managed_video_runtime(product: Path) -> Path:
    return product / "install/downloads/Olivia-video-runtime-private.zip"


def test_first_install_consumes_only_bundled_core_assets() -> None:
    script = (ROOT / "installer" / "Install.ps1").read_text(encoding="utf-8-sig")

    assert "offline\\offline-core-assets.json" in script
    assert "python-3.12.10-embed-amd64.zip" in script
    assert "pip-25.2-py3-none-any.whl" in script
    assert "--no-index" in script
    assert "--find-links" in script
    assert "Invoke-WebRequest" not in script
    assert "python.org" not in script
    assert "bootstrap.pypa.io" not in script
    assert script.count("mem0-runtime-requirements.txt") == 1
    assert "[IO.Directory]::Exists($MemoryRuntimePath)" in script
    assert "Update-ManagedPythonPath -PthPath $pth.FullName" in script
    assert "provision_mem0_embedding.py" not in script
    assert "BAAI/bge-small-zh-v1.5" not in script
    assert "VOICE_REFERENCE_PRIVATE_MANIFEST_REQUIRED" in script
    assert "Install-ManagedVideoRuntime -VideoRuntime $coreAssets.VideoRuntime" in script


def test_installer_accepts_only_complete_private_video_runtime_manifest() -> None:
    script = (ROOT / "installer" / "Install.ps1").read_text(encoding="utf-8-sig")

    assert "$hasVideoRuntime = $manifest.PSObject.Properties.Name -ccontains 'video_runtime'" in script
    assert "$hasVideoRuntime -ne $hasVoiceReference" in script
    assert "$manifestNames += @('distribution', 'voice_reference', 'video_runtime')" in script
    assert "Assert-OfflineObjectShape -Value $manifest.video_runtime -Names @('path', 'size_bytes', 'sha256')" in script
    assert "$manifest.video_runtime.path -cne 'video-runtime/Olivia-video-runtime-private.zip'" in script
    assert "$videoRuntimePath = Resolve-OfflineAsset -Root $Root -Asset $manifest.video_runtime" in script
    assert "verified = [bool]$true" in script
    assert "VideoRuntime = $videoRuntime" in script


def test_offline_core_asset_example_matches_its_public_schema() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "offline_core_assets.schema.json").read_text(
            encoding="utf-8"
        )
    )
    example = json.loads(
        (ROOT / "contracts" / "offline_core_assets.example.json").read_text(
            encoding="utf-8"
        )
    )

    Draft202012Validator.check_schema(schema)
    assert not list(Draft202012Validator(schema).iter_errors(example))


def test_public_schema_accepts_only_complete_hash_locked_private_assets() -> None:
    schema = json.loads((ROOT / "contracts/offline_core_assets.schema.json").read_text())
    example = json.loads((ROOT / "contracts/offline_core_assets.example.json").read_text())
    validator = Draft202012Validator(schema)
    example["distribution"] = "private"
    example["voice_reference"] = {"path": "voice/olivia-reference.wav", "size_bytes": 155278, "sha256": "7bd846a55265d5ceb4dcf0ef164dc954066b8b056ac1e40d554b1e41d844a5bf", "wave": _wave_metadata(frame_count=77600)}
    example["video_runtime"] = {
        "path": "video-runtime/Olivia-video-runtime-private.zip",
        "size_bytes": 1,
        "sha256": "0" * 64,
    }
    assert not list(validator.iter_errors(example))
    missing_runtime = deepcopy(example)
    del missing_runtime["video_runtime"]
    assert list(validator.iter_errors(missing_runtime))
    missing_marker = deepcopy(example)
    del missing_marker["distribution"]
    assert list(validator.iter_errors(missing_marker))
    wrong_path = deepcopy(example)
    wrong_path["voice_reference"]["path"] = "voice/arbitrary.wav"
    assert list(validator.iter_errors(wrong_path))
    extra_field = deepcopy(example)
    extra_field["voice_reference"]["license"] = "caller-asserted"
    assert list(validator.iter_errors(extra_field))


def test_public_schema_rejects_nonfixed_runtime_and_incomplete_wheel_closure() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "offline_core_assets.schema.json").read_text(
            encoding="utf-8"
        )
    )
    example = json.loads(
        (ROOT / "contracts" / "offline_core_assets.example.json").read_text(
            encoding="utf-8"
        )
    )
    validator = Draft202012Validator(schema)

    wrong_runtime = deepcopy(example)
    wrong_runtime["python_runtime"]["path"] = "python-arbitrary.zip"
    assert list(validator.iter_errors(wrong_runtime))

    wrong_runtime_hash = deepcopy(example)
    wrong_runtime_hash["python_runtime"]["sha256"] = "0" * 64
    assert list(validator.iter_errors(wrong_runtime_hash))

    incomplete = deepcopy(example)
    incomplete["wheels"] = incomplete["wheels"][:-1]
    assert list(validator.iter_errors(incomplete))

    extra_field = deepcopy(example)
    extra_field["python_runtime"]["unexpected"] = True
    assert list(validator.iter_errors(extra_field))

    missing_source = deepcopy(example)
    del missing_source["python_runtime"]["source_url"]
    assert list(validator.iter_errors(missing_source))

    insecure_source = deepcopy(example)
    insecure_source["python_runtime"]["source_url"] = "http://mirror.example/python.zip"
    assert list(validator.iter_errors(insecure_source))

    wrong_pip_hash = deepcopy(example)
    wrong_pip_hash["pip_bootstrap"]["sha256"] = "0" * 64
    assert list(validator.iter_errors(wrong_pip_hash))

    traversal = deepcopy(example)
    traversal["wheels"][0]["path"] = "../escape.whl"
    assert list(validator.iter_errors(traversal))

    duplicate_path = deepcopy(example)
    duplicate_path["wheels"][1]["path"] = duplicate_path["wheels"][0]["path"]
    assert list(validator.iter_errors(duplicate_path))

    duplicate_hash = deepcopy(example)
    duplicate_hash["wheels"][1]["sha256"] = duplicate_hash["wheels"][0]["sha256"]
    assert list(validator.iter_errors(duplicate_hash))


def test_first_install_rebuilds_and_atomically_replaces_any_existing_runtime() -> None:
    script = (ROOT / "installer" / "Install.ps1").read_text(encoding="utf-8-sig")

    assert "if (-not (Test-Path -LiteralPath $runtimeExe))" not in script
    assert "$productRoot = [IO.Path]::GetFullPath($Destination)" in script
    assert "$Destination = Join-Path $productRoot 'install'" in script
    assert "$runtimeRoot = Join-Path $productRoot 'runtime\\python-3.12.10-embed-amd64'" in script
    assert "GetFileName($destinationFull" not in script
    assert "Join-Path $env:LOCALAPPDATA 'BSideOliviaLocal\\runtime" not in script
    assert "Assert-OfflineObjectShape" in script
    assert "OFFLINE_CORE_WHEEL_SET_MISMATCH" in script
    assert "$lockedWheelHashes.SetEquals($manifestWheelHashes)" in script
    assert "[IO.Directory]::Move($runtimeRoot, $runtimeBackup)" in script
    assert "[IO.Directory]::Move($runtimeBackup, $runtimeRoot)" in script
    assert script.rindex(
        "$selectedOfficial = Resolve-OfficialInstall"
    ) < script.index("$coreAssets = Get-OfflineCoreAssets")
    assert script.rindex("Assert-OfficialSource") < script.index(
        "$coreAssets = Get-OfflineCoreAssets"
    )


def _run_install_preflight(
    product_root: Path,
    tmp_path: Path,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "installer" / "Install.ps1"),
            "-PayloadRoot",
            str(ROOT),
            "-OfflineAssetsRoot",
            str(tmp_path / "missing-offline-assets"),
            "-Destination",
            str(product_root),
            "-SkipShortcut",
            *extra,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _run_runtime_publish_fixture(
    tmp_path: Path,
    *,
    bootstrap_exit_code: int, bootstrap_replaces_managed_app: bool = False,
    voice_reference: bytes | None = None, voice_reference_sha256: str | None = None,
    voice_reference_wave: dict[str, object] | None = None, voice_reference_missing: bool = False,
    video_runtime: bytes | None = None, video_runtime_sha256: str | None = None,
    video_runtime_missing: bool = False,
    video_runtime_verified: bool = True, preinstalled_video_runtime: bytes | None = None,
    stale_video_backup: bytes | None = None, reject_video_source_rehash: bool = False,
    block_video_cleanup: bool = False,
    block_voice_sidecar: bool = False, cleanup_obstruction: str | None = None,
    product_root: Path | None = None, existing_voice_pair: bool = False,
    interrupt_voice_staging: bool = False, interrupt_after_bootstrap: bool = False, existing_runtime: bool = True, seed_existing_install: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    payload = tmp_path / "payload"
    payload_installer = payload / "installer"
    payload_installer.mkdir(parents=True)
    for name in (
        "full-patch-manifest.json",
        "runtime-requirements.txt",
        "mem0-runtime-requirements.txt",
        "verify_mem0_runtime.py",
    ):
        shutil.copy2(ROOT / "installer" / name, payload_installer / name)
    bootstrap_actions = ""
    if bootstrap_exit_code == 0 or bootstrap_replaces_managed_app:
        bootstrap_actions = "destination = pathlib.Path(sys.argv[sys.argv.index('--destination') + 1])\n" \
                            "(destination / 'data').mkdir(parents=True, exist_ok=True)\n"
        if bootstrap_replaces_managed_app:
            bootstrap_actions += "shutil.rmtree(destination / 'local_backend', ignore_errors=True)\n" \
                "(destination / 'local_backend').mkdir()\n" \
                "(destination / 'local_backend/new-backend.txt').write_text('new')\n"
    (payload_installer / "bootstrap_install.py").write_text(
        "import json, pathlib, shutil, sys\n"
        + bootstrap_actions
        + f"print(json.dumps({{'status': '{'OK' if bootstrap_exit_code == 0 else 'ERROR'}', 'code': 'SYNTHETIC_PATCH_FAILED'}}))\n"
        f"raise SystemExit({bootstrap_exit_code})\n",
        encoding="utf-8",
    )
    official = tmp_path / "official"
    resources = official / "0.0.9.627" / "resources"
    resources.mkdir(parents=True)
    (official / "launcher.exe").write_bytes(b"launcher")
    (official / "0.0.9.627" / "Olivia.exe").write_bytes(b"client")
    (resources / "feapp.dat").write_bytes(b"feapp")
    (resources / "webplayer.dat").write_bytes(b"webplayer")

    runtime_zip = tmp_path / "runtime.zip"
    python_executable = Path(getattr(sys, "_base_executable", sys.executable))
    python_home = python_executable.parent
    python_tag = f"python{sys.version_info.major}{sys.version_info.minor}"
    with zipfile.ZipFile(runtime_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(python_executable, "python.exe")
        for name in (f"{python_tag}.dll", "python3.dll", "vcruntime140.dll"):
            candidate = python_home / name
            if candidate.is_file():
                archive.write(candidate, name)
        archive.writestr(
            f"{python_tag}._pth",
            "\n".join(
                [
                    str(python_home / f"{python_tag}.zip"),
                    str(python_home),
                    str(python_home / "Lib"),
                    str(python_home / "Lib" / "site-packages"),
                    "import site",
                ]
            ),
        )

    script = (ROOT / "installer" / "Install.ps1").read_text(encoding="utf-8-sig")
    original = "$coreAssets = Get-OfflineCoreAssets -Root $offlineRoot -ManifestPath $offlineManifestPath -RequirementsPath $requirements"
    replacement = (
        "$voiceManifest = if ($env:BSIDE_TEST_PRIVATE_MANIFEST) { [IO.File]::ReadAllText($env:BSIDE_TEST_PRIVATE_MANIFEST) | ConvertFrom-Json } else { $null }\n"
        "$voiceReference = if ($voiceManifest) { $voiceManifest.voice_reference.path = $env:BSIDE_TEST_VOICE_REFERENCE; $voiceManifest.voice_reference } else { $null }\n"
        "$videoRuntime = if ($voiceManifest) { $voiceManifest.video_runtime.path = $env:BSIDE_TEST_VIDEO_RUNTIME; $voiceManifest.video_runtime } else { $null }\n"
        "$coreAssets = @{ Runtime = $env:BSIDE_TEST_RUNTIME_ZIP; PipBootstrap = ''; Wheelhouse = ''; VoiceReference = $voiceReference; VideoRuntime = $videoRuntime }"
    )
    if reject_video_source_rehash:
        replacement = (
            "$script:OriginalGetSha256 = ${function:Get-Sha256}\n"
            "function Get-Sha256 { param([Parameter(Mandatory)][string]$LiteralPath) "
            "if ($LiteralPath -ceq $env:BSIDE_TEST_VIDEO_RUNTIME) { throw 'VIDEO_RUNTIME_SOURCE_REHASHED' }; "
            "& $script:OriginalGetSha256 -LiteralPath $LiteralPath }\n"
            + replacement
        )
    assert script.count(original) == 1
    script = script.replace(original, replacement)
    dependency_probe = "if (-not (Test-ManagedServerDependencies -PythonExe $candidateExe)) {"
    assert script.count(dependency_probe) == 2
    script = script.replace(dependency_probe, "if ($false) {", 1)
    if cleanup_obstruction == "voice":
        marker = "} elseif ($phase -ceq 'cleanup' -and [IO.Directory]::Exists($shared)) { Repair-ManagedVoiceTransaction -SharedRoot $shared }"
        obstruction = marker.replace("Repair-Managed", "$voiceLock = [IO.File]::Open((Get-ChildItem -LiteralPath $shared -Filter '*.wav.bak' | Select-Object -First 1).FullName, 'Open', 'Read', 'None'); Repair-Managed")
        assert script.count(marker) == 1
        script = script.replace(marker, obstruction)
    elif cleanup_obstruction == "snapshot":
        marker = "if ($Snapshot -and (Test-Path -LiteralPath $Snapshot)) { Remove-Item -LiteralPath $Snapshot -Recurse -Force }"
        assert script.count(marker) == 1
        script = script.replace(marker, marker.replace("Remove-Item", "$lock = [IO.File]::Open((Join-Path $Snapshot '.locked'), 'Create', 'ReadWrite', 'None'); Remove-Item"))
    if interrupt_voice_staging:
        marker = "        [IO.File]::Copy($source, $stagedTarget, $false)"
        assert script.count(marker) == 1
        script = script.replace(marker, marker + "\n        [Environment]::FailFast('SYNTHETIC_VOICE_INTERRUPTION')")
    if interrupt_after_bootstrap:
        marker = "$installExitCode = $LASTEXITCODE"
        assert script.count(marker) == 1
        script = script.replace(marker, "[Environment]::FailFast('SYNTHETIC_BOOTSTRAP_INTERRUPTION')\n" + marker)
    if block_video_cleanup:
        marker = "Complete-ManagedVideoRuntimeTransaction -Transaction $videoRuntimeTransaction"
        obstruction = (
            "if ($videoRuntimeTransaction.Backup) { "
            "[IO.File]::Delete([string]$videoRuntimeTransaction.Backup); "
            "[IO.Directory]::CreateDirectory([string]$videoRuntimeTransaction.Backup) | Out-Null }\n"
            + marker
        )
        assert script.count(marker) == 1
        script = script.replace(marker, obstruction)
    test_script = tmp_path / "Install.ps1"
    test_script.write_text(script, encoding="utf-8-sig")
    product = product_root or tmp_path / "product"
    old_runtime = product / "runtime" / "python-3.12.10-embed-amd64"
    if existing_runtime and not old_runtime.exists():
        old_runtime.mkdir(parents=True)
        (old_runtime / "old-runtime.txt").write_text("preserve", encoding="utf-8")
    if preinstalled_video_runtime is not None:
        installed_video_runtime = _managed_video_runtime(product)
        installed_video_runtime.parent.mkdir(parents=True, exist_ok=True)
        installed_video_runtime.write_bytes(preinstalled_video_runtime)
        if stale_video_backup is not None:
            (installed_video_runtime.parent / (".Olivia-video-runtime." + "0" * 32 + ".bak")).write_bytes(
                stale_video_backup
            )
    if bootstrap_replaces_managed_app and seed_existing_install and not (product / "install/local_backend").exists():
        old_backend = product / "install" / "local_backend"
        old_backend.mkdir(parents=True)
        (old_backend / "old-backend.txt").write_text("preserve", encoding="utf-8")
        old_video_runtime = _managed_video_runtime(product)
        old_video_runtime.parent.mkdir(parents=True)
        old_video_runtime.write_bytes(b"old-video-runtime")
    if block_voice_sidecar:
        installed = _managed_reference(product)
        installed.parent.mkdir(parents=True)
        installed.write_bytes(b"old-reference")
        installed.with_suffix(".json").mkdir()
    if existing_voice_pair:
        installed = _managed_reference(product)
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_bytes(b"old-reference")
        installed.with_suffix(".json").write_bytes(b"old-sidecar")
    environment = os.environ.copy()
    environment["BSIDE_TEST_RUNTIME_ZIP"] = str(runtime_zip)
    if voice_reference is not None:
        if video_runtime is None:
            video_runtime = b"video-runtime-fixture"
        reference = tmp_path / "distributor-reference.wav"
        if not voice_reference_missing:
            reference.write_bytes(voice_reference)
        runtime_archive = tmp_path / "Olivia-video-runtime-private.zip"
        if not video_runtime_missing:
            runtime_archive.write_bytes(video_runtime)
        manifest = {"voice_reference": {"path": "voice/olivia-reference.wav", "size_bytes": len(voice_reference),
                    "sha256": voice_reference_sha256 or hashlib.sha256(voice_reference).hexdigest(),
                    "wave": voice_reference_wave if voice_reference_wave is not None else _wave_metadata()},
                    "video_runtime": {"path": "video-runtime/Olivia-video-runtime-private.zip",
                                       "size_bytes": len(video_runtime),
                                       "sha256": video_runtime_sha256 or hashlib.sha256(video_runtime).hexdigest(),
                                       "verified": video_runtime_verified}}
        manifest_path = tmp_path / "offline-core-assets.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        environment["BSIDE_TEST_VOICE_REFERENCE"] = str(reference)
        environment["BSIDE_TEST_VIDEO_RUNTIME"] = str(runtime_archive)
        environment["BSIDE_TEST_PRIVATE_MANIFEST"] = str(manifest_path)
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
            "-PayloadRoot",
            str(payload),
            "-Destination",
            str(product),
            "-OfficialRoot",
            str(official),
            "-NonInteractive",
            "-SkipShortcut",
        ],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if cleanup_obstruction == "voice":
        for blocked in _managed_reference(product).parent.glob(".linli-reference.*"):
            if blocked.is_dir():
                shutil.rmtree(blocked)
    return result, product


def test_first_install_publishes_voice_reference_to_preserved_data_path(tmp_path: Path) -> None:
    result, product = _run_runtime_publish_fixture(tmp_path, bootstrap_exit_code=0, voice_reference=_voice_reference_bytes())

    installed = _managed_reference(product)
    assert result.returncode == 0, result.stderr or result.stdout
    assert installed.read_bytes() == _voice_reference_bytes()
    assert _managed_video_runtime(product).read_bytes() == b"video-runtime-fixture"
    integrity = json.loads(installed.with_suffix(".json").read_text(encoding="utf-8"))
    assert integrity == {"schema_version": "olivia.managed-voice-reference.v1", "path": "linli-reference.wav",
        "size_bytes": installed.stat().st_size, "sha256": hashlib.sha256(installed.read_bytes()).hexdigest(),
        "wave": _wave_metadata()}
    schema = json.loads((ROOT / "contracts/managed_voice_reference.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    assert not list(Draft202012Validator(schema).iter_errors(integrity))
def test_voice_publish_failure_restores_managed_app_runtime_and_voice(tmp_path: Path) -> None:
    result, product = _run_runtime_publish_fixture(
        tmp_path, bootstrap_exit_code=0, bootstrap_replaces_managed_app=True,
        voice_reference=_voice_reference_bytes(), block_voice_sidecar=True,
    )
    installed = _managed_reference(product)
    runtime = product / "runtime/python-3.12.10-embed-amd64"; backend = product / "install/local_backend"
    assert result.returncode != 0 and "VOICE_REFERENCE_INSTALL_FAILED" in result.stdout + result.stderr
    assert (backend / "old-backend.txt").read_text() == "preserve" and not (backend / "new-backend.txt").exists()
    assert (runtime / "old-runtime.txt").read_text() == "preserve" and not (runtime / "python.exe").exists()
    assert installed.read_bytes() == b"old-reference" and installed.with_suffix(".json").is_dir()
    assert _managed_video_runtime(product).read_bytes() == b"old-video-runtime"
    assert not list(product.glob(".install.rollback.*")) and not list((product / "runtime").glob("*.backup.*"))


@pytest.mark.parametrize("cleanup_obstruction", ["voice", "snapshot"])
def test_voice_reference_cleanup_failure_is_fail_closed_and_recovered(
    tmp_path: Path, cleanup_obstruction: str,
) -> None:
    product = tmp_path / "product"
    failed, _ = _run_runtime_publish_fixture(
        tmp_path / "first", product_root=product, bootstrap_exit_code=0,
        bootstrap_replaces_managed_app=True, voice_reference=_voice_reference_bytes(), existing_voice_pair=True,
        cleanup_obstruction=cleanup_obstruction,
    )
    shared = _managed_reference(product).parent
    assert failed.returncode != 0 and "VOICE_REFERENCE_INSTALL_CLEANUP_FAILED" in failed.stdout + failed.stderr
    assert (shared / ".linli-reference.transaction.cleanup").is_file() or list(product.glob(".install.rollback.*"))
    assert (product / "install/local_backend/new-backend.txt").is_file() and (product / "runtime/python-3.12.10-embed-amd64/python.exe").is_file()
    assert _managed_reference(product).read_bytes() == _voice_reference_bytes()
    recovered, _ = _run_runtime_publish_fixture(
        tmp_path / "second", product_root=product, bootstrap_exit_code=23,
        bootstrap_replaces_managed_app=True, voice_reference=_voice_reference_bytes(),
    )
    assert recovered.returncode == 23
    assert (product / "install/local_backend/new-backend.txt").is_file() and (product / "runtime/python-3.12.10-embed-amd64/python.exe").is_file()
    assert not list(shared.glob(".linli-reference.*")) and not list(product.glob(".install.rollback.*"))


@pytest.mark.parametrize(
    ("content", "wave_metadata", "missing", "sha256", "code"),
    [
        (_voice_reference_bytes(), None, True, None, "VOICE_REFERENCE_MISSING"),
        (_voice_reference_bytes(), None, False, "0" * 64, "VOICE_REFERENCE_HASH_MISMATCH"),
        (b"not a wave", None, False, None, "VOICE_REFERENCE_INVALID"),
        (_voice_reference_bytes(), _wave_metadata(frame_count=159), False, None, "VOICE_REFERENCE_INVALID"),
        (_voice_reference_bytes(), _wave_metadata(channels="1"), False, None, "VOICE_REFERENCE_INVALID"),
    ],
)
def test_voice_reference_fails_closed_before_publish(
    tmp_path: Path, content: bytes, wave_metadata: dict[str, object] | None,
    missing: bool, sha256: str | None, code: str,
) -> None:
    result, product = _run_runtime_publish_fixture(
        tmp_path, bootstrap_exit_code=0, voice_reference=content,
        voice_reference_wave=wave_metadata, voice_reference_missing=missing,
        voice_reference_sha256=sha256,
    )
    assert result.returncode != 0
    assert code in result.stdout + result.stderr
    assert not _managed_reference(product).exists()


def test_interrupted_voice_staging_has_marker_and_is_recovered(tmp_path: Path) -> None:
    product = tmp_path / "product"
    interrupted, _ = _run_runtime_publish_fixture(
        tmp_path / "first", product_root=product, bootstrap_exit_code=0,
        voice_reference=_voice_reference_bytes(), existing_voice_pair=True,
        interrupt_voice_staging=True,
    )
    installed = _managed_reference(product)
    assert interrupted.returncode != 0 and (installed.parent / ".linli-reference.transaction").is_file()
    assert list(installed.parent.glob(".linli-reference.*.wav.tmp"))

    rejected, _ = _run_runtime_publish_fixture(
        tmp_path / "second", product_root=product, bootstrap_exit_code=0,
        voice_reference=b"not a wave",
    )
    assert rejected.returncode != 0 and "VOICE_REFERENCE_INVALID" in rejected.stdout + rejected.stderr
    assert installed.read_bytes() == b"old-reference" and installed.with_suffix(".json").read_bytes() == b"old-sidecar"
    assert not list(installed.parent.glob(".linli-reference.*"))


def test_first_install_rejects_bad_video_runtime_before_publish(tmp_path: Path) -> None:
    result, product = _run_runtime_publish_fixture(
        tmp_path,
        bootstrap_exit_code=0,
        voice_reference=_voice_reference_bytes(),
        video_runtime_sha256="0" * 64,
    )

    assert result.returncode != 0
    assert "VIDEO_RUNTIME_HASH_MISMATCH" in result.stdout + result.stderr
    assert not _managed_video_runtime(product).exists()


def test_matching_video_runtime_skips_a_missing_payload_archive(tmp_path: Path) -> None:
    runtime = b"video-runtime-fixture"
    result, product = _run_runtime_publish_fixture(
        tmp_path,
        bootstrap_exit_code=0,
        voice_reference=_voice_reference_bytes(),
        video_runtime=runtime,
        video_runtime_missing=True,
        preinstalled_video_runtime=runtime,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert _managed_video_runtime(product).read_bytes() == runtime


def test_matching_video_runtime_recovers_a_stale_backup_on_retry(tmp_path: Path) -> None:
    runtime = b"video-runtime-fixture"
    result, product = _run_runtime_publish_fixture(
        tmp_path,
        bootstrap_exit_code=0,
        voice_reference=_voice_reference_bytes(),
        video_runtime=runtime,
        video_runtime_missing=True,
        preinstalled_video_runtime=runtime,
        stale_video_backup=b"old-video-runtime",
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert _managed_video_runtime(product).read_bytes() == runtime
    assert not list(_managed_video_runtime(product).parent.glob(".Olivia-video-runtime.*.bak"))


def test_video_runtime_publish_rejects_an_unverified_test_asset(tmp_path: Path) -> None:
    result, product = _run_runtime_publish_fixture(
        tmp_path,
        bootstrap_exit_code=0,
        voice_reference=_voice_reference_bytes(),
        video_runtime_verified=False,
    )

    assert result.returncode != 0
    assert "VIDEO_RUNTIME_INVALID" in result.stdout + result.stderr
    assert not _managed_video_runtime(product).exists()


def test_fresh_video_runtime_does_not_rehash_the_verified_source(tmp_path: Path) -> None:
    result, product = _run_runtime_publish_fixture(
        tmp_path,
        bootstrap_exit_code=0,
        voice_reference=_voice_reference_bytes(),
        reject_video_source_rehash=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert _managed_video_runtime(product).read_bytes() == b"video-runtime-fixture"


def test_video_runtime_cleanup_failure_is_deferred_without_undoing_publish(
    tmp_path: Path,
) -> None:
    result, product = _run_runtime_publish_fixture(
        tmp_path,
        bootstrap_exit_code=0,
        bootstrap_replaces_managed_app=True,
        voice_reference=_voice_reference_bytes(),
        block_video_cleanup=True,
    )

    output = result.stdout + result.stderr
    target = _managed_video_runtime(product)
    assert result.returncode == 0, output
    assert "VIDEO_RUNTIME_INSTALL_CLEANUP_DEFERRED" in output
    assert target.read_bytes() == b"video-runtime-fixture"
    assert (product / "install/local_backend/new-backend.txt").is_file()
    assert (product / "runtime/python-3.12.10-embed-amd64/python.exe").is_file()
    assert not list(product.glob(".install.rollback.*"))
    assert not list((product / "runtime").glob("python-3.12.10-embed-amd64.backup.*"))
    blocked = list(target.parent.glob(".Olivia-video-runtime.*.bak"))
    assert len(blocked) == 1 and blocked[0].is_dir()
    shutil.rmtree(blocked[0])


def test_patch_failure_restores_existing_runtime_and_cleans_transaction_paths(
    tmp_path: Path,
) -> None:
    result, product = _run_runtime_publish_fixture(
        tmp_path, bootstrap_exit_code=23, bootstrap_replaces_managed_app=True,
    )

    runtime_parent = product / "runtime"
    runtime = runtime_parent / "python-3.12.10-embed-amd64"
    backend = product / "install/local_backend"
    assert result.returncode == 23, result.stderr or result.stdout
    assert (backend / "old-backend.txt").is_file() and not (backend / "new-backend.txt").exists()
    assert (runtime / "old-runtime.txt").read_text(encoding="utf-8") == "preserve"
    assert not (runtime / "python.exe").exists()
    assert not list(runtime_parent.glob("python-3.12.10-embed-amd64.backup.*"))
    assert not list(runtime_parent.glob("python-3.12.10-embed-amd64.staging.*"))
    fresh, fresh_product = _run_runtime_publish_fixture(tmp_path / "fresh", bootstrap_exit_code=23, bootstrap_replaces_managed_app=True, existing_runtime=False, seed_existing_install=False)
    assert fresh.returncode == 23 and not (fresh_product / "install").exists() and not (fresh_product / "runtime/python-3.12.10-embed-amd64").exists()


def test_interrupted_bootstrap_recovers_original_install_on_reentry(tmp_path: Path) -> None:
    product = tmp_path / "product"
    interrupted, _ = _run_runtime_publish_fixture(tmp_path / "first", product_root=product, bootstrap_exit_code=0, bootstrap_replaces_managed_app=True, interrupt_after_bootstrap=True)
    assert interrupted.returncode != 0
    retried, _ = _run_runtime_publish_fixture(tmp_path / "second", product_root=product, bootstrap_exit_code=23, bootstrap_replaces_managed_app=True)
    backend = product / "install/local_backend"
    assert retried.returncode == 23
    assert (backend / "old-backend.txt").is_file() and not (backend / "new-backend.txt").exists()
    assert not list(product.glob(".install.*"))


def test_patch_success_discards_runtime_backup_after_install(
    tmp_path: Path,
) -> None:
    result, product = _run_runtime_publish_fixture(
        tmp_path,
        bootstrap_exit_code=0,
    )

    runtime_parent = product / "runtime"
    runtime = runtime_parent / "python-3.12.10-embed-amd64"
    assert result.returncode == 0, result.stderr or result.stdout
    assert (runtime / "python.exe").is_file()
    assert not (runtime / "old-runtime.txt").exists()
    assert not list(runtime_parent.glob("python-3.12.10-embed-amd64.backup.*"))
    assert not list(runtime_parent.glob("python-3.12.10-embed-amd64.staging.*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
@pytest.mark.parametrize("product_name", ["isolated-product", "install"])
def test_first_install_rejects_a_reparse_point_at_the_selected_product_root(
    tmp_path: Path,
    product_name: str,
) -> None:
    outside = tmp_path / "outside"
    product_root = tmp_path / product_name
    outside.mkdir()
    linked = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(product_root), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if linked.returncode != 0:
        pytest.skip("junction creation is unavailable")
    try:
        result = _run_install_preflight(product_root, tmp_path)

        assert result.returncode != 0
        assert "OFFLINE_CORE_RUNTIME_PARENT_INVALID" in result.stdout + result.stderr
        assert not (outside / "runtime").exists()
    finally:
        os.rmdir(product_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_first_install_rejects_a_reparse_point_in_an_existing_ancestor(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    linked_ancestor = tmp_path / "linked-ancestor"
    outside.mkdir()
    linked = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(linked_ancestor), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if linked.returncode != 0:
        pytest.skip("junction creation is unavailable")
    try:
        result = _run_install_preflight(linked_ancestor / "Olivia", tmp_path)

        assert result.returncode != 0
        assert "OFFLINE_CORE_RUNTIME_PARENT_INVALID" in result.stdout + result.stderr
        assert not (outside / "Olivia" / "runtime").exists()
    finally:
        os.rmdir(linked_ancestor)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_first_install_rejects_an_official_junction_alias_before_writes(
    tmp_path: Path,
) -> None:
    physical_official = tmp_path / "physical-official"
    official_alias = tmp_path / "official-alias"
    product_root = physical_official / "nested-product"
    physical_official.mkdir()
    linked = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(official_alias), str(physical_official)],
        capture_output=True,
        text=True,
        check=False,
    )
    if linked.returncode != 0:
        pytest.skip("junction creation is unavailable")
    try:
        result = _run_install_preflight(
            product_root,
            tmp_path,
            "-OfficialRoot",
            str(official_alias),
        )

        assert result.returncode != 0
        assert "OFFICIAL_INSTALL_PATH_REPARSE_POINT" in result.stdout + result.stderr
        assert not (product_root / "runtime").exists()
    finally:
        os.rmdir(official_alias)


@pytest.mark.skipif(os.name != "nt", reason="Windows path contract")
def test_first_install_rejects_a_volume_root_as_product_root(tmp_path: Path) -> None:
    result = _run_install_preflight(Path(tmp_path.anchor), tmp_path)

    assert result.returncode != 0
    assert "OFFLINE_CORE_RUNTIME_PARENT_INVALID" in result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows preflight contract")
def test_first_install_rejects_official_overlap_before_runtime_writes(
    tmp_path: Path,
) -> None:
    product_root = tmp_path / "official"
    product_root.mkdir()

    result = _run_install_preflight(
        product_root,
        tmp_path,
        "-OfficialRoot",
        str(product_root),
    )

    assert result.returncode != 0
    assert "INSTALL_ROOT_OVERLAPS_OFFICIAL" in result.stdout + result.stderr
    assert not (product_root / "runtime").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows preflight contract")
def test_first_install_defers_official_archive_compatibility_to_python_installer(
    tmp_path: Path,
) -> None:
    product_root = tmp_path / "product"
    official_root = tmp_path / "official"
    resources = official_root / "0.0.9.627" / "resources"
    resources.mkdir(parents=True)
    (official_root / "launcher.exe").write_bytes(b"launcher")
    (official_root / "0.0.9.627" / "Olivia.exe").write_bytes(b"client")
    (resources / "feapp.dat").write_bytes(b"wrong feapp")
    (resources / "webplayer.dat").write_bytes(b"wrong webplayer")

    result = _run_install_preflight(
        product_root,
        tmp_path,
        "-OfficialRoot",
        str(official_root),
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "OFFLINE_CORE_ASSETS_MISSING" in output
    assert "UNSUPPORTED_OFFICIAL_VERSION" not in output
    assert not (product_root / "runtime").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows setup diagnostic contract")
def test_failed_setup_records_selected_official_archives_and_manifest_hashes(
    tmp_path: Path,
) -> None:
    product_root = tmp_path / "product"
    official_root = tmp_path / "official"
    resources = official_root / "0.0.9.627" / "resources"
    resources.mkdir(parents=True)
    (official_root / "launcher.exe").write_bytes(b"launcher")
    (official_root / "0.0.9.627" / "Olivia.exe").write_bytes(b"client")
    feapp = resources / "feapp.dat"
    webplayer = resources / "webplayer.dat"
    feapp.write_bytes(b"observed feapp")
    webplayer.write_bytes(b"observed webplayer")
    result_path = tmp_path / "setup-result.txt"

    result = _run_install_preflight(
        product_root,
        tmp_path,
        "-OfficialRoot",
        str(official_root),
        "-SetupResultPath",
        str(result_path),
    )

    assert result.returncode != 0
    diagnostic_path = Path(str(result_path) + ".diagnostic.json")
    diagnostic_text = diagnostic_path.read_text(encoding="utf-8")
    diagnostic = json.loads(diagnostic_text)
    manifest = json.loads(
        (ROOT / "installer" / "full-patch-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert diagnostic == {
        "schema_version": "olivia.setup-source-diagnostic.v1",
        "selection_mode": "explicit",
        "candidate_count": 1,
        "selected_official_id": hashlib.sha256(
            str(official_root.resolve()).rstrip("\\").lower().encode("utf-8")
        ).hexdigest()[:16],
        "client_version": "0.0.9.627",
        "observed_feapp_size": feapp.stat().st_size,
        "observed_feapp_sha256": hashlib.sha256(feapp.read_bytes()).hexdigest(),
        "observed_webplayer_size": webplayer.stat().st_size,
        "observed_webplayer_sha256": hashlib.sha256(
            webplayer.read_bytes()
        ).hexdigest(),
        "manifest_feapp_sha256": manifest["feapp_sha256"],
        "manifest_webplayer_sha256": manifest["webplayer_sha256"],
    }
    assert str(official_root) not in diagnostic_text


def test_offline_asset_builder_uses_the_hash_locked_windows_wheel_closure() -> None:
    builder = (ROOT / "installer" / "build_offline_core_assets.py").read_text(
        encoding="utf-8"
    )

    assert "--require-hashes" in builder
    assert "--only-binary=:all:" in builder
    assert "--no-deps" in builder
    assert "--platform" in builder and "win_amd64" in builder
    assert "--python-version" in builder and "3.12" in builder
    assert "--abi" in builder and "cp312" in builder
    assert "python-3.12.10-embed-amd64.zip" in builder
    assert "pip-25.2-py3-none-any.whl" in builder
    assert "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3" in builder
    assert "6d67a2b4e7f14d8b31b8b52648866fa717f45a1eb70e83002f4331d07e953717" in builder
