from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

from jsonschema import Draft202012Validator
import pytest


ROOT = Path(__file__).resolve().parents[2]


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
    bootstrap_exit_code: int,
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
    (payload_installer / "bootstrap_install.py").write_text(
        "import json\n"
        f"print(json.dumps({{'status': 'ERROR', 'code': 'SYNTHETIC_PATCH_FAILED'}}))\n"
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
    replacement = "$coreAssets = @{ Runtime = $env:BSIDE_TEST_RUNTIME_ZIP; PipBootstrap = ''; Wheelhouse = '' }"
    assert script.count(original) == 1
    script = script.replace(original, replacement)
    dependency_probe = "if (-not (Test-ManagedServerDependencies -PythonExe $candidateExe)) {"
    assert script.count(dependency_probe) == 2
    script = script.replace(dependency_probe, "if ($false) {", 1)
    test_script = tmp_path / "Install.ps1"
    test_script.write_text(script, encoding="utf-8-sig")
    product = tmp_path / "product"
    old_runtime = product / "runtime" / "python-3.12.10-embed-amd64"
    old_runtime.mkdir(parents=True)
    (old_runtime / "old-runtime.txt").write_text("preserve", encoding="utf-8")
    environment = os.environ.copy()
    environment["BSIDE_TEST_RUNTIME_ZIP"] = str(runtime_zip)
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
    return result, product


def test_patch_failure_restores_existing_runtime_and_cleans_transaction_paths(
    tmp_path: Path,
) -> None:
    result, product = _run_runtime_publish_fixture(
        tmp_path,
        bootstrap_exit_code=23,
    )

    runtime_parent = product / "runtime"
    runtime = runtime_parent / "python-3.12.10-embed-amd64"
    assert result.returncode == 23, result.stderr or result.stdout
    assert (runtime / "old-runtime.txt").read_text(encoding="utf-8") == "preserve"
    assert not (runtime / "python.exe").exists()
    assert not list(runtime_parent.glob("python-3.12.10-embed-amd64.backup.*"))
    assert not list(runtime_parent.glob("python-3.12.10-embed-amd64.staging.*"))


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
