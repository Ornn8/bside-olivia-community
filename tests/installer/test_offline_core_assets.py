from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator


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
