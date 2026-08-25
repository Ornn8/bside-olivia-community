"""Canonical-letter candidate analysis is optional and never mutates the ledger."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from llm_gateway import Gateway, GatewayResponse
from private_world_candidates import (
    CandidateStatus,
    CandidateType,
    SQLitePrivateWorldCandidateStore,
    candidate_identity,
)
from private_world_delivery import PrivateWorldDeliveryCommitter
from private_world_ledger import SQLitePrivateWorldLedger
from private_world_port import PrivateWorldSnapshot
from reply_context import ReplyMode
from reply_orchestrator import ReplyState
from reply_pipeline import PipelineResult


ROOT = Path(__file__).resolve().parents[2]


class AcceptedPipeline:
    async def run(self, request: object, context: object) -> PipelineResult:
        return PipelineResult(
            "candidate-letter",
            ReplyState.COMPLETED,
            text="synthetic canonical reply",
            quality_status="accepted",
        )


class TextTriage:
    reply_mode = ReplyMode.TEXT_LETTER.value

    def to_dict(self) -> dict[str, str]:
        return {"reply_mode": self.reply_mode}


class TextTriageService:
    async def classify(self, content: str) -> TextTriage:
        return TextTriage()


def test_canonical_reply_creates_pending_candidate_without_mutating_snapshot(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import local_server
    from private_world_candidate import PrivateWorldCandidateProposal

    database = tmp_path / "private-world.sqlite3"
    ledger = SQLitePrivateWorldLedger(database)
    candidate_store = SQLitePrivateWorldCandidateStore(database)
    snapshot_before = ledger.snapshot()
    requests: list[object] = []

    class SyntheticAnalyzer:
        async def analyze(self, request: object) -> PrivateWorldCandidateProposal:
            requests.append(request)
            return PrivateWorldCandidateProposal(
                candidate_type=CandidateType.BOUNDARY_RESPECTED,
                confidence=0.8,
                summary="synthetic bounded candidate summary",
            )

    letter = {
        "letter_id": "candidate-letter",
        "content": "synthetic current letter",
        "reply_text": "",
        "letter_status": "PENDING",
    }
    monkeypatch.setattr(local_server.store, "letters", [letter])
    monkeypatch.setattr(local_server, "emotion_triage", TextTriageService())
    monkeypatch.setattr(local_server, "reply_pipeline", AcceptedPipeline())
    monkeypatch.setattr(
        local_server,
        "private_world_committer",
        PrivateWorldDeliveryCommitter(ledger),
    )
    monkeypatch.setattr(local_server, "private_world_port", ledger)
    monkeypatch.setattr(local_server.letters_adapter, "private_world_port", ledger)
    monkeypatch.setattr(local_server, "private_world_candidate_analyzer", SyntheticAnalyzer())
    monkeypatch.setattr(local_server, "private_world_candidate_store", candidate_store)
    monkeypatch.setattr(local_server, "_schedule_text_reply_delay", lambda *args: None)
    monkeypatch.setattr(local_server, "_persist_store_state", lambda: None)
    monkeypatch.setattr(
        local_server.letters_adapter,
        "remember_conversation",
        lambda *args: None,
    )

    async def run() -> None:
        assert await local_server.generate_reply(
            "candidate-letter", "synthetic current letter"
        ) is True
        await local_server.wait_for_private_world_candidate_tasks()

    asyncio.run(run())

    candidate = candidate_store.get(
        candidate_identity(
            "candidate-letter", 1, CandidateType.BOUNDARY_RESPECTED
        ),
        now=datetime.now(timezone.utc),
    )
    assert candidate is not None
    assert candidate.status is CandidateStatus.PENDING
    assert candidate.summary == "synthetic bounded candidate summary"
    assert ledger.snapshot() == snapshot_before
    assert len(requests) == 1

    blocked_letter = {
        "letter_id": "candidate-without-delivery",
        "content": "synthetic current letter",
        "reply_text": "",
        "letter_status": "PENDING",
    }
    monkeypatch.setattr(local_server.store, "letters", [blocked_letter])
    monkeypatch.setattr(local_server, "private_world_committer", None)

    async def run_without_delivery() -> None:
        assert await local_server.generate_reply(
            "candidate-without-delivery", "synthetic current letter"
        ) is True
        await local_server.wait_for_private_world_candidate_tasks()

    asyncio.run(run_without_delivery())

    assert blocked_letter["letter_status"] == "COMPLETED"
    assert blocked_letter["private_world_status"] == "PENDING"
    assert len(requests) == 1
    assert len(candidate_store.list_candidates()) == 1


def test_gateway_analyzer_returns_only_a_bounded_low_privilege_proposal() -> None:
    from private_world_candidate import (
        GatewayPrivateWorldCandidateAnalyzer,
        PrivateWorldCandidateRequest,
    )

    class RecordingGateway(Gateway):
        def __init__(self) -> None:
            self.messages: object = None

        async def complete(
            self, messages: object, *, request_id: str | None = None
        ) -> GatewayResponse:
            self.messages = messages
            return GatewayResponse(
                text=(
                    '{"schema_version":"p03.private-world-candidate.v1",'
                    '"candidate":"boundary_respected","confidence":0.8,'
                    '"summary":"synthetic bounded candidate summary",'
                    '"evidence_spans":[]}'
                ),
                request_id=request_id or "candidate-test",
                provider="synthetic",
                model="synthetic",
            )

    gateway = RecordingGateway()
    analyzer = GatewayPrivateWorldCandidateAnalyzer(gateway, timeout_seconds=1.0)
    request = PrivateWorldCandidateRequest.create(
        source_letter_id="candidate-letter",
        source_reply_revision=1,
        user_message="synthetic current letter",
        canonical_reply="synthetic canonical reply",
        character_view=PrivateWorldSnapshot(
            trust=88,
            comfort=77,
        ).character_view(),
        occurred_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )

    proposal = asyncio.run(analyzer.analyze(request))

    assert proposal is not None
    assert proposal.candidate_type is CandidateType.BOUNDARY_RESPECTED
    assert proposal.confidence == 0.8
    assert proposal.summary == "synthetic bounded candidate summary"
    serialized = str(gateway.messages)
    assert "synthetic current letter" in serialized
    assert "synthetic canonical reply" in serialized
    assert "88" not in serialized
    assert "77" not in serialized
    assert "relationship_stage" in serialized
    assert "candidate-letter" not in serialized
    assert "source_reply_revision" not in serialized


def test_candidate_delivery_is_idempotent_and_analysis_failure_is_nonblocking(
    tmp_path: Path,
) -> None:
    from private_world_candidate import (
        CandidateDeliveryStatus,
        PrivateWorldCandidateProposal,
        PrivateWorldCandidateRequest,
        deliver_private_world_candidate,
    )

    database = tmp_path / "private-world.sqlite3"
    SQLitePrivateWorldLedger(database)
    candidate_store = SQLitePrivateWorldCandidateStore(database)
    request = PrivateWorldCandidateRequest.create(
        source_letter_id="candidate-idempotency",
        source_reply_revision=1,
        user_message="synthetic current letter",
        canonical_reply="synthetic canonical reply",
        character_view=PrivateWorldSnapshot().character_view(),
        occurred_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )

    class SyntheticAnalyzer:
        async def analyze(self, _request: object) -> PrivateWorldCandidateProposal:
            return PrivateWorldCandidateProposal(
                CandidateType.REPAIR,
                0.8,
                "synthetic bounded candidate summary",
            )

    class FailingAnalyzer:
        async def analyze(self, _request: object) -> None:
            raise RuntimeError("synthetic provider failure")

    created = asyncio.run(
        deliver_private_world_candidate(SyntheticAnalyzer(), candidate_store, request)
    )
    duplicate = asyncio.run(
        deliver_private_world_candidate(SyntheticAnalyzer(), candidate_store, request)
    )
    unavailable = asyncio.run(
        deliver_private_world_candidate(FailingAnalyzer(), candidate_store, request)
    )

    assert (created, duplicate, unavailable) == (
        CandidateDeliveryStatus.CREATED,
        CandidateDeliveryStatus.DUPLICATE,
        CandidateDeliveryStatus.UNAVAILABLE,
    )
    assert len(candidate_store.list_candidates()) == 1


def test_candidate_runtime_requires_explicit_enablement_and_ready_dependencies(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import private_world_candidate as candidate_module
    from private_world_candidate import (
        GatewayPrivateWorldCandidateAnalyzer,
        NullPrivateWorldCandidateAnalyzer,
        create_private_world_candidate_runtime,
    )

    class UnusedGateway(Gateway):
        async def complete(
            self, messages: object, *, request_id: str | None = None
        ) -> GatewayResponse:
            raise AssertionError("runtime construction must not call the provider")

    database = tmp_path / "private-world.sqlite3"
    SQLitePrivateWorldLedger(database)
    disabled = create_private_world_candidate_runtime(
        UnusedGateway(),
        database_path=database,
        gateway_ready=True,
        environ={},
    )
    available = create_private_world_candidate_runtime(
        UnusedGateway(),
        database_path=database,
        gateway_ready=True,
        environ={"OLIVIA_PRIVATE_WORLD_CANDIDATES_ENABLED": "1"},
    )

    assert disabled.status == "disabled"
    assert isinstance(disabled.analyzer, NullPrivateWorldCandidateAnalyzer)
    assert disabled.store is None
    assert available.status == "available"
    assert isinstance(available.analyzer, GatewayPrivateWorldCandidateAnalyzer)
    assert available.store is not None
    assert available.public_status() == {
        "status": "available",
        "provider": "llm_gateway",
        "reason_code": None,
        "enabled": True,
        "network_called": False,
    }

    monkeypatch.setattr(
        candidate_module,
        "_SCHEMA_PATH",
        tmp_path / "missing-candidate-schema.json",
    )
    schema_unavailable = create_private_world_candidate_runtime(
        UnusedGateway(),
        database_path=database,
        gateway_ready=True,
        environ={"OLIVIA_PRIVATE_WORLD_CANDIDATES_ENABLED": "1"},
    )
    assert schema_unavailable.public_status() == {
        "status": "unavailable",
        "provider": "none",
        "reason_code": "PRIVATE_WORLD_CANDIDATE_SCHEMA_UNAVAILABLE",
        "enabled": True,
        "network_called": False,
    }


def test_local_server_wires_explicit_candidate_runtime_and_sanitized_health(
    tmp_path: Path,
) -> None:
    script = """
import asyncio
import json
import local_server

health = asyncio.run(
    local_server.route('GET', '/health', {}, {'profile': 'core'})
)['data']['providers']['private_world_candidates']
print(json.dumps({
    'runtime_status': local_server.private_world_candidate_runtime.status,
    'analyzer_type': type(local_server.private_world_candidate_analyzer).__name__,
    'store_type': type(local_server.private_world_candidate_store).__name__,
    'health': health,
}, sort_keys=True))
"""
    environment = os.environ.copy()
    environment.update(
        {
            "OLIVIA_LOCAL_DATA_ROOT": str(tmp_path / "local-data"),
            "OLIVIA_LLM_PROVIDER": "mock",
            "OLIVIA_LLM_FEATURE_ENABLED": "1",
            "OLIVIA_MEMORY_ENABLED": "0",
            "OLIVIA_PRIVATE_WORLD_CANDIDATES_ENABLED": "1",
            "PYTHONPATH": str(ROOT)
            + os.pathsep
            + environment.get("PYTHONPATH", ""),
        }
    )
    environment.pop("OLIVIA_PRIVATE_WORLD_DB", None)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload == {
        "runtime_status": "available",
        "analyzer_type": "GatewayPrivateWorldCandidateAnalyzer",
        "store_type": "SQLitePrivateWorldCandidateStore",
        "health": {
            "status": "available",
            "provider": "llm_gateway",
            "reason_code": None,
            "enabled": True,
            "network_called": False,
        },
    }
