from __future__ import annotations

from pathlib import Path

from installer.start_local import _configure_memory_environment
from local_memory import LocalMemoryAdapter, create_memory_adapter
from mem0_memory import load_mem0_config
from memory_port import LEGACY_LETTERS, LegacyLetter
from private_world_ledger import SQLitePrivateWorldLedger


def _base() -> dict[str, str]:
    return {
        "OLIVIA_LLM_BASE_URL": "https://api.example.invalid",
        "OLIVIA_LLM_MODEL": "fixture-model",
        "OLIVIA_LLM_API_KEY_ENV": "FIXTURE_API_KEY",
    }


def test_normal_start_enables_confined_offline_memory_by_default(
    tmp_path: Path,
) -> None:
    environment = _configure_memory_environment(_base(), tmp_path / "data")
    assert environment["OLIVIA_MEMORY_ENABLED"] == "1"
    assert environment["OLIVIA_MEMORY_ROOT"] == str(
        tmp_path / "data" / "memory"
    )
    assert environment["OLIVIA_CONVERSATION_MEMORY_ROOT"] == str(
        tmp_path / "data" / "memory" / "mem0"
    )
    assert environment["OLIVIA_MEMORY_EMBEDDING_CACHE"] == str(
        tmp_path / "data" / "memory" / "model-cache"
    )
    assert environment["FASTEMBED_CACHE_PATH"] == environment[
        "OLIVIA_MEMORY_EMBEDDING_CACHE"
    ]
    assert environment["OLIVIA_MEMORY_EMBEDDING_MODEL"] == (
        "BAAI/bge-small-zh-v1.5"
    )
    assert environment["OLIVIA_MEMORY_EMBEDDING_DIMS"] == "512"
    assert environment["OLIVIA_MEMORY_LLM_BASE_URL"] == (
        "https://api.example.invalid"
    )
    assert environment["OLIVIA_MEMORY_LLM_MODEL"] == "fixture-model"
    assert environment["OLIVIA_MEMORY_LLM_API_KEY_ENV"] == (
        "FIXTURE_API_KEY"
    )
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["HF_HUB_DISABLE_TELEMETRY"] == "1"


def test_memory_reuses_the_primary_key_variable_that_is_actually_set(
    tmp_path: Path,
) -> None:
    environment = _base()
    environment["OLIVIA_LLM_API_KEY"] = "fixture-secret"
    configured = _configure_memory_environment(
        environment,
        tmp_path / "data",
    )
    assert configured["OLIVIA_MEMORY_LLM_API_KEY_ENV"] == (
        "OLIVIA_LLM_API_KEY"
    )
    assert "fixture-secret" not in repr(
        {
            key: value
            for key, value in configured.items()
            if key.endswith("_API_KEY_ENV")
        }
    )


def test_explicit_memory_disable_is_preserved(tmp_path: Path) -> None:
    environment = _base()
    environment["OLIVIA_MEMORY_ENABLED"] = "0"
    configured = _configure_memory_environment(environment, tmp_path / "data")
    assert configured["OLIVIA_MEMORY_ENABLED"] == "0"


def test_windows_installer_keeps_memory_optional_and_verified() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "installer" / "Install.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "memory-runtime-requirements.txt" in script
    assert "--require-hashes" in script
    assert "--only-binary=:all:" in script
    assert "memory-model-manifest.json" in script
    assert "provision_memory_model.py" in script
    assert "--verify-only" in script
    assert "--provision" in script
    assert "Olivia will continue without long-term memory" in script
    assert "MEMORY_DEPENDENCIES_UNAVAILABLE" in script
    assert "MEMORY_MODEL_UNAVAILABLE" in script
    assert "if (-not $?) { exit 2 }" in script
    assert script.rstrip().endswith("exit 0")

    lock = (root / "installer" / "memory-runtime-requirements.txt").read_text(
        encoding="utf-8"
    )
    package_lines = [
        line for line in lock.splitlines()
        if line and not line.startswith("#")
    ]
    assert package_lines
    assert all("==" in line and "--hash=sha256:" in line for line in package_lines)
    assert any(line.startswith("mem0ai==2.0.18 ") for line in package_lines)
    assert any(line.startswith("fastembed==0.8.0 ") for line in package_lines)


def test_archive_mem0_and_private_world_keep_distinct_roots(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    archive_root = data_root / "memory"
    archive_db = archive_root / "memory.sqlite3"
    original = LocalMemoryAdapter(archive_db)
    try:
        result = original.import_legacy_records(
            [
                LegacyLetter(
                    content="升级前保留的旧信证据。",
                    source_record_id="legacy-before-mem0",
                    source="fixture",
                )
            ]
        )
        assert result.inserted == 1
    finally:
        original.close()

    environment = _configure_memory_environment(_base(), data_root)
    archive = create_memory_adapter(environ=environment)
    try:
        records = archive.search(
            "旧信证据",
            domains=(LEGACY_LETTERS,),
            limit=4,
        )
        assert [record.text for record in records] == [
            "升级前保留的旧信证据。"
        ]
        assert getattr(archive, "db_path", None) == archive_db
    finally:
        close = getattr(archive, "close", None)
        if callable(close):
            close()

    mem0 = load_mem0_config(environ=environment, project_root=tmp_path)
    private_world_db = data_root / "private_world" / "private_world.sqlite3"
    ledger = SQLitePrivateWorldLedger(private_world_db)
    assert ledger.snapshot().version >= 1

    assert mem0.data_root == archive_root / "mem0"
    assert mem0.qdrant_path.is_relative_to(mem0.data_root)
    assert archive_db.parent == archive_root
    assert private_world_db.parent == data_root / "private_world"
    assert not archive_db.is_relative_to(mem0.data_root)
    assert not private_world_db.is_relative_to(mem0.data_root)
