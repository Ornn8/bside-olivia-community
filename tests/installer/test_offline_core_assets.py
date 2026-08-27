from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess

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
    assert "mem0-runtime-requirements.txt" not in script
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
def test_first_install_validates_official_files_before_runtime_writes(
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
    assert "UNSUPPORTED_OFFICIAL_VERSION" in result.stdout + result.stderr
    assert not (product_root / "runtime").exists()


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
