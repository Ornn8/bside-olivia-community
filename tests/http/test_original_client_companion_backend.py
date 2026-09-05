from __future__ import annotations

from datetime import datetime, timezone
import inspect
from pathlib import Path

import pytest

from conversation_memory_admin import MemoryAdminStatus
from conversation_memory_port import ConversationMemoryRecord
from original_client_companion_backend import (
    OriginalClientCompanionBackendError,
    OriginalClientCompanionServiceBackend,
)
from private_world_candidates import (
    CandidateStatus,
    CandidateType,
    PrivateWorldCandidate,
)


NOW = datetime(2026, 8, 23, 10, 30, tzinfo=timezone.utc)


def test_status_history_retains_timeout_after_recovery() -> None:
    from dataclasses import replace
    from original_client_server import _diagnostic_source
    from runtime.diagnostics.support_bundle import build_diagnostic_bundle
    import io, json, zipfile

    class RecoveringMemory(MemoryAdminFixture):
        failed = True

        def status(self):
            result = super().status()
            return replace(result, status="unavailable", reason_code="MEM0_SEARCH_TIMEOUT") if self.failed else result

    memory = RecoveringMemory()
    backend = OriginalClientCompanionServiceBackend(memory_admin=memory)
    backend.read_status()
    memory.failed = False
    source = _diagnostic_source(backend, setup_service=None, launcher_tail_provider=None, runtime_tail_provider=None)()
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        records = [json.loads(line) for line in archive.read("runtime-tail.jsonl").splitlines()]
    checks = [item for item in records if item["event"] == "companion_memory_status"]
    assert [item["status"] for item in checks] == ["unavailable", "available"]
    assert checks[0]["error_code"] == "MEM0_SEARCH_TIMEOUT"
    assert all(isinstance(item["elapsed_ms"], int) and item["elapsed_ms"] >= 0 for item in checks)
    assert all(set(item) <= {"event", "status", "error_code", "elapsed_ms", "recorded_at_ms"} for item in checks)


class MemoryAdminFixture:
    def __init__(self) -> None:
        self.requests: list[tuple[str | None, int]] = []

    def status(self) -> MemoryAdminStatus:
        return MemoryAdminStatus(
            status="available",
            provider="mem0",
            enabled=True,
            memory_count=2,
            audit_count=0,
            pending_correction_count=0,
        )

    def list_memories(
        self,
        *,
        query: str | None = None,
        limit: int = 100,
    ):
        self.requests.append((query, limit))
        return (
            ConversationMemoryRecord(
                memory_id="memory.1",
                text="用户喜欢雨天散步。",
                user_id="local-user",
                source_id="reply:letter-1:1",
                created_at=NOW,
                score=0.9,
            ),
            ConversationMemoryRecord(
                memory_id="memory.2",
                text="用户计划九月去云南。",
                user_id="local-user",
                source_id="reply:letter-2:1",
                occurred_at=NOW,
            ),
        )[:limit]


class PrivateWorldFixture:
    def snapshot(self):
        return {
            "schema_version": "p03.private-world-control.v1",
            "version": 8,
            "relationship_stage": "trusted_friend",
            "levels": {
                "familiarity": "medium",
                "trust": "high",
                "comfort": "medium",
                "closeness": "medium",
                "tension": "low",
            },
            "nickname_permissions": ["小河豚"],
            "home_access": "visit_access",
            "continuation_facts": [
                {
                    "fact_id": "continuation.yunnan",
                    "statement": "林离正在准备云南采风。",
                    "awareness": "character_known",
                }
            ],
            "hidden_scores": {"trust": 88, "comfort": 72},
            "database_path": "C:/private/private_world.sqlite3",
        }


class CandidateFixture:
    def __init__(self) -> None:
        self.calls: list[tuple[CandidateStatus | None, datetime | None]] = []

    def list_candidates(
        self,
        *,
        status: CandidateStatus | None = None,
        now: datetime | None = None,
    ):
        self.calls.append((status, now))
        return (
            PrivateWorldCandidate(
                candidate_id="candidate.repair.1",
                source_letter_id="letter-1",
                source_reply_revision=1,
                candidate_type=CandidateType.REPAIR,
                summary="双方完成了一次明确修复。",
                confidence=0.98,
                status=CandidateStatus.PENDING,
                created_at=NOW,
                expires_at=datetime(
                    2026,
                    8,
                    30,
                    10,
                    30,
                    tzinfo=timezone.utc,
                ),
            ),
        )


def test_adapter_maps_existing_services_without_hidden_state() -> None:
    memory = MemoryAdminFixture()
    world = PrivateWorldFixture()
    candidates = CandidateFixture()
    backend = OriginalClientCompanionServiceBackend(
        memory_admin=memory,
        private_world=world,
        candidates=candidates,
        now=lambda: NOW,
    )

    status = backend.read_status()
    assert status.memory.to_dict() == {"state": "available", "count": 2}
    assert status.private_world.to_dict() == {"state": "available"}
    assert status.candidates.to_dict() == {"state": "available", "count": 1}

    memories = backend.list_memories(query="雨天", limit=2)
    assert memory.requests == [("雨天", 2)]
    assert [value.created_at for value in memories] == [NOW.isoformat()] * 2
    assert memories[0].score == 0.9

    summary = backend.private_world_summary().to_dict()
    assert summary["version"] == 8
    assert summary["levels"]["trust"] == "high"
    assert summary["nickname_permissions"] == ["小河豚"]
    assert summary["continuation_facts"][0]["awareness"] == "character_known"
    serialized = repr(summary)
    assert "hidden_scores" not in serialized
    assert "database_path" not in serialized
    assert "88" not in serialized

    pending = backend.list_candidates(limit=5)
    assert candidates.calls[-1] == (CandidateStatus.PENDING, NOW)
    assert pending[0].candidate_type == "repair"
    candidate_payload = pending[0].to_dict()
    assert "confidence" not in candidate_payload
    assert "source_letter_id" not in candidate_payload


def test_absent_services_are_independently_disabled() -> None:
    backend = OriginalClientCompanionServiceBackend(now=lambda: NOW)

    status = backend.read_status().to_dict()["capabilities"]
    assert status == {
        "memory": {
            "state": "disabled",
            "reason_code": "COMPANION_MEMORY_DISABLED",
        },
        "private_world": {
            "state": "disabled",
            "reason_code": "COMPANION_PRIVATE_WORLD_DISABLED",
        },
        "candidates": {
            "state": "disabled",
            "reason_code": "COMPANION_CANDIDATES_DISABLED",
        },
    }

    with pytest.raises(OriginalClientCompanionBackendError) as memory_error:
        backend.list_memories(query=None, limit=10)
    assert memory_error.value.code == "COMPANION_MEMORY_DISABLED"

    with pytest.raises(OriginalClientCompanionBackendError) as world_error:
        backend.private_world_summary()
    assert world_error.value.code == "COMPANION_PRIVATE_WORLD_DISABLED"

    with pytest.raises(OriginalClientCompanionBackendError) as candidate_error:
        backend.list_candidates(limit=10)
    assert candidate_error.value.code == "COMPANION_CANDIDATES_DISABLED"


def test_service_failures_degrade_only_the_affected_capability() -> None:
    class FailingMemory(MemoryAdminFixture):
        def status(self) -> MemoryAdminStatus:
            raise OSError("private memory path")

    class FailingCandidates(CandidateFixture):
        def list_candidates(self, *, status=None, now=None):
            raise OSError("private candidate path")

    backend = OriginalClientCompanionServiceBackend(
        memory_admin=FailingMemory(),
        private_world=PrivateWorldFixture(),
        candidates=FailingCandidates(),
        now=lambda: NOW,
    )

    capabilities = backend.read_status().to_dict()["capabilities"]
    assert capabilities["memory"] == {
        "state": "unavailable",
        "reason_code": "COMPANION_MEMORY_UNAVAILABLE",
    }
    assert capabilities["private_world"] == {"state": "available"}
    assert capabilities["candidates"] == {
        "state": "unavailable",
        "reason_code": "COMPANION_CANDIDATES_UNAVAILABLE",
    }


def test_invalid_provider_records_do_not_gain_invented_fields() -> None:
    class UndatedMemory(MemoryAdminFixture):
        def list_memories(self, *, query=None, limit=100):
            return (
                ConversationMemoryRecord(
                    memory_id="memory.undated",
                    text="没有时间字段的记忆。",
                    user_id="local-user",
                    source_id="reply:undated:1",
                ),
            )

    backend = OriginalClientCompanionServiceBackend(
        memory_admin=UndatedMemory(),
        now=lambda: NOW,
    )
    with pytest.raises(OriginalClientCompanionBackendError) as error:
        backend.list_memories(query=None, limit=10)
    assert error.value.code == "COMPANION_MEMORY_TIME_UNAVAILABLE"

    class InvalidWorld(PrivateWorldFixture):
        def snapshot(self):
            payload = dict(super().snapshot())
            payload["levels"] = {"trust": "high"}
            return payload

    invalid_world = OriginalClientCompanionServiceBackend(
        private_world=InvalidWorld(),
        now=lambda: NOW,
    )
    with pytest.raises(OriginalClientCompanionBackendError) as world_error:
        invalid_world.private_world_summary()
    assert world_error.value.code == "COMPANION_PRIVATE_WORLD_INVALID"


def test_adapter_uses_services_instead_of_storage_implementations() -> None:
    module = __import__("original_client_companion_backend")
    source = inspect.getsource(module).casefold()
    forbidden = (
        "import sqlite3",
        "qdrant",
        "sqlite3.connect",
        ".execute(",
        ".cursor(",
    )
    assert not any(value in source for value in forbidden)

    module_path = Path(module.__file__ or "")
    assert module_path.name == "original_client_companion_backend.py"
