from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

from local_memory import LocalMemoryAdapter
from memory_port import CONVERSATION_MEMORY, LEGACY_LETTERS, LegacyLetter, NullMemoryPort
from memory_prompt import MEMORY_CONTEXT_BEGIN
from runtime.packaging.b10b.errors import B10BError
from runtime.packaging.b10b.live_bridge import build_live_service_from_b10b
from runtime.packaging.b10b.manager import B10BManager


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "B10B live memory bridge project"
    project.mkdir()
    (project / "local_server.py").write_text("# B02 contract\n", encoding="utf-8")
    (project / "http_contract.py").write_text("# B02 contract\n", encoding="utf-8")
    return project, tmp_path / "B10B live memory bridge data"


def _enabled_live_manager(tmp_path: Path) -> B10BManager:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    modules = ["core/http", "asr-local", "visual-driver", "tts-local", "live-orchestration"]
    manager.install(modules)
    for module in modules:
        manager.enable(module)
    return manager


def test_enabled_memory_library_is_retrievable_but_never_becomes_conversation_memory(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _enabled_live_manager(tmp_path)
    database = tmp_path / "legacy-library" / "memory.sqlite3"
    adapter = LocalMemoryAdapter(database)
    try:
        adapter.import_legacy_records(
            [LegacyLetter("bridge legacy reference", "fixture-legacy-1", "fixture-source")]
        )
        content_hash = next(iter(adapter.legacy_content_hashes()))
    finally:
        adapter.close()

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        connection.close()
    database.with_name(database.name + "-wal").unlink(missing_ok=True)
    database.with_name(database.name + "-shm").unlink(missing_ok=True)
    before_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    before_mtime = database.stat().st_mtime_ns

    monkeypatch.setattr(
        "runtime.packaging.b10b.manager.is_external_reference", lambda _value, **_kwargs: True
    )
    manager.install(["memory-local"])
    manager.enable("memory-local")
    manager.customize("memory-local", {"database_path": str(database)})

    service = build_live_service_from_b10b(
        project_root=manager.project_root,
        data_root=manager.data_root,
        environ={"OLIVIA_LLM_PROVIDER": "mock"},
    )
    async def exercise():
        try:
            session = await service.start_session("fixture-owner")
            messages, memory_status = session._build_messages("bridge", "fixture-turn")
            result = await session.send_text("bridge")
            return messages[-1]["content"], memory_status, result, service.memory_port.status()
        finally:
            await service.stop()

    user_message, memory_status, result, status = asyncio.run(exercise())

    assert result.completed
    assert result.memory_status == "available"
    assert memory_status == "available"
    assert MEMORY_CONTEXT_BEGIN in user_message
    assert "LEGACY_LETTERS_REFERENCE_ONLY" in user_message
    assert content_hash in user_message
    assert "fixture-source" in user_message
    assert "CONVERSATION_MEMORY_CURRENT" not in user_message
    assert status["conversation_enabled"] is False
    assert status["read_only"] is True
    assert status["counts"][LEGACY_LETTERS] == 1
    assert status["counts"][CONVERSATION_MEMORY] == 0
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_hash
    assert database.stat().st_mtime_ns == before_mtime
    assert not database.with_name(database.name + "-wal").exists()
    assert not database.with_name(database.name + "-shm").exists()


def test_disabled_memory_module_forces_truthful_session_only_even_with_environment_root(
    tmp_path: Path,
) -> None:
    manager = _enabled_live_manager(tmp_path)
    database = tmp_path / "project-config-library" / "memory.sqlite3"
    adapter = LocalMemoryAdapter(database)
    try:
        adapter.import_legacy_records(
            [LegacyLetter("project config must not leak", "fixture-disabled-1", "fixture-project")]
        )
    finally:
        adapter.close()
    (manager.project_root / "memory_config.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "provider": "sqlite",
                "data_root": str(database.parent),
            }
        ),
        encoding="utf-8",
    )
    service = build_live_service_from_b10b(
        project_root=manager.project_root,
        data_root=manager.data_root,
        environ={
            "OLIVIA_LLM_PROVIDER": "mock",
            "OLIVIA_MEMORY_ENABLED": "true",
            "OLIVIA_MEMORY_ROOT": str(tmp_path / "unowned-memory"),
        },
    )
    try:
        session = asyncio.run(service.start_session("fixture-owner"))
        messages, memory_status = session._build_messages("bridge", "fixture-turn")

        assert memory_status == "session-only"
        assert MEMORY_CONTEXT_BEGIN not in messages[-1]["content"]
        assert "project config must not leak" not in messages[-1]["content"]
        assert isinstance(service.memory_port, NullMemoryPort)
        assert service.memory_port.status()["status"] == "disabled"
    finally:
        asyncio.run(service.stop())


def test_enabled_memory_rejects_an_unowned_sqlite_without_exposing_its_path(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _enabled_live_manager(tmp_path)
    database = tmp_path / "not-a-b04-library.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    database.touch()
    monkeypatch.setattr(
        "runtime.packaging.b10b.manager.is_external_reference", lambda _value, **_kwargs: True
    )
    manager.install(["memory-local"])
    manager.enable("memory-local")
    manager.customize("memory-local", {"database_path": str(database)})

    try:
        build_live_service_from_b10b(
            project_root=manager.project_root,
            data_root=manager.data_root,
            environ={"OLIVIA_LLM_PROVIDER": "mock"},
        )
    except B10BError as exc:
        assert exc.code == "MEMORY_DATABASE_INVALID"
        assert str(database) not in str(exc.details)
    else:
        raise AssertionError("unowned SQLite must be rejected")
