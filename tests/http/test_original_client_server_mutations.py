from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from control_center.private_world_candidate_api import (
    CandidateDecisionRequest,
    CandidateDecisionResult,
)
from control_center.private_world_candidate_backend import (
    SQLiteCandidateReviewBackend,
)
from conversation_memory_admin import (
    MemoryAdminMutationResult,
    MemoryAdminMutationStatus,
    MemoryAdminStatus,
)
from conversation_memory_port import NullConversationMemoryPort
from original_client_companion_mutation_api import (
    CONFIRM_HEADER,
    CONFIRM_VALUE,
)
from original_client_server import (
    create_configured_original_client_server_runtime,
    create_original_client_server_runtime,
)
from private_world_delivery import PrivateWorldDeliveryCommitter
from private_world_ledger import SQLitePrivateWorldLedger


TRUSTED_ORIGIN = "https://client.example"


async def _fallback(request: web.Request) -> web.Response:
    return web.json_response({"fallback": request.path})


class MemoryAdminFixture:
    def __init__(self) -> None:
        self.corrected: list[tuple[str, str, str, str]] = []
        self.deleted: list[tuple[str, str, str]] = []

    def status(self) -> MemoryAdminStatus:
        return MemoryAdminStatus("available", "fixture", True, 0, 0, 0)

    def list_memories(self, *, query=None, limit=100):
        return ()

    def correct(
        self,
        memory_id: str,
        corrected_text: str,
        *,
        request_id: str,
        reason: str,
    ) -> MemoryAdminMutationResult:
        self.corrected.append((memory_id, corrected_text, request_id, reason))
        return MemoryAdminMutationResult(
            MemoryAdminMutationStatus.APPLIED,
            request_id,
            "correct",
            affected_count=2,
            target_memory_id=memory_id,
            replacement_memory_id="memory.corrected.1",
        )

    def delete(
        self,
        memory_id: str,
        *,
        request_id: str,
        reason: str,
    ) -> MemoryAdminMutationResult:
        self.deleted.append((memory_id, request_id, reason))
        return MemoryAdminMutationResult(
            MemoryAdminMutationStatus.APPLIED,
            request_id,
            "delete",
            affected_count=1,
            target_memory_id=memory_id,
        )


class CandidateDecisionFixture:
    def __init__(self) -> None:
        self.requests: list[CandidateDecisionRequest] = []

    def decide(
        self,
        request: CandidateDecisionRequest,
    ) -> CandidateDecisionResult:
        self.requests.append(request)
        return CandidateDecisionResult(
            candidate_id=request.candidate_id,
            decision=request.decision,
            status="approved" if request.decision == "approve" else "rejected",
            reason_code=(
                "PRIVATE_WORLD_CANDIDATE_APPROVED"
                if request.decision == "approve"
                else "PRIVATE_WORLD_CANDIDATE_REJECTED"
            ),
        )


def test_original_runtime_mounts_direct_memory_and_candidate_mutations() -> None:
    async def scenario() -> None:
        memory = MemoryAdminFixture()
        candidates = CandidateDecisionFixture()
        runtime = create_original_client_server_runtime(
            _fallback,
            memory_admin=memory,
            candidate_decisions=candidates,
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(runtime.app)) as client:
            missing_confirmation = await client.post(
                "/toy/companion/memory/delete",
                json={
                    "memory_id": "memory.fixture.1",
                    "request_id": "request.memory.delete.1",
                    "reason": "用户明确删除。",
                },
                headers={"Origin": TRUSTED_ORIGIN},
            )
            assert missing_confirmation.status == 403
            assert (await missing_confirmation.json())["error_code"] == (
                "COMPANION_CONFIRMATION_REQUIRED"
            )

            corrected = await client.post(
                "/toy/companion/memory/correct",
                json={
                    "memory_id": "memory.fixture.1",
                    "replacement_text": "用户现在住在东京北区。",
                    "request_id": "request.memory.correct.1",
                    "reason": "用户明确纠正。",
                },
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    CONFIRM_HEADER: CONFIRM_VALUE,
                },
            )
            assert corrected.status == 200
            assert (await corrected.json())["status"] == "APPLIED"

            deleted = await client.post(
                "/toy/companion/memory/delete",
                json={
                    "memory_id": "memory.fixture.2",
                    "request_id": "request.memory.delete.1",
                    "reason": "用户明确删除。",
                },
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    CONFIRM_HEADER: CONFIRM_VALUE,
                },
            )
            assert deleted.status == 200
            assert (await deleted.json())["affected_count"] == 1

            approved = await client.post(
                "/toy/companion/private-world/candidates/candidate.fixture.1/approve",
                json={
                    "request_id": "request.candidate.approve.1",
                    "reason": "用户明确确认。",
                    "decided_at": "2026-08-23T12:00:00+00:00",
                },
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    CONFIRM_HEADER: CONFIRM_VALUE,
                },
            )
            assert approved.status == 200
            assert (await approved.json())["status"] == "APPLIED"

            fallback = await client.get("/health")
            assert fallback.status == 200
            assert await fallback.json() == {"fallback": "/health"}

        assert memory.corrected == [
            (
                "memory.fixture.1",
                "用户现在住在东京北区。",
                "request.memory.correct.1",
                "用户明确纠正。",
            )
        ]
        assert memory.deleted == [
            (
                "memory.fixture.2",
                "request.memory.delete.1",
                "用户明确删除。",
            )
        ]
        assert candidates.requests == [
            CandidateDecisionRequest(
                candidate_id="candidate.fixture.1",
                decision="approve",
                request_id="request.candidate.approve.1",
                reason="用户明确确认。",
                decided_at="2026-08-23T12:00:00+00:00",
            )
        ]

    asyncio.run(scenario())


def test_configured_runtime_reuses_existing_private_world_ledger_for_decisions(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        root = tmp_path / "data"
        root.mkdir()
        database = root / "private_world" / "private_world.sqlite3"
        ledger = SQLitePrivateWorldLedger(database)
        server = SimpleNamespace(
            handler=_fallback,
            letters_adapter=SimpleNamespace(
                memory_prompt_builder=SimpleNamespace(
                    conversation_memory=NullConversationMemoryPort(),
                    conversation_memory_user_id="local-user",
                )
            ),
            private_world_port=ledger,
            private_world_committer=PrivateWorldDeliveryCommitter(ledger),
            TRUSTED_FRONTEND_ORIGINS=frozenset({TRUSTED_ORIGIN}),
        )
        runtime = create_configured_original_client_server_runtime(
            server_module=server,
            environ={
                "OLIVIA_LOCAL_DATA_ROOT": str(root),
                "OLIVIA_PRIVATE_WORLD_ENABLED": "1",
                "OLIVIA_PRIVATE_WORLD_DB": str(database),
            },
        )
        assert isinstance(
            runtime.candidate_decisions,
            SQLiteCandidateReviewBackend,
        )
        assert runtime.candidate_decisions.command_service._ledger is ledger

        async with TestClient(TestServer(runtime.app)) as client:
            missing = await client.post(
                "/toy/companion/private-world/candidates/candidate.missing.1/approve",
                json={
                    "request_id": "request.candidate.missing.1",
                    "reason": "用户明确确认。",
                    "decided_at": "2026-08-23T12:00:00+00:00",
                },
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    CONFIRM_HEADER: CONFIRM_VALUE,
                },
            )
            assert missing.status == 404
            assert (await missing.json())["error_code"] == (
                "PRIVATE_WORLD_CANDIDATE_NOT_FOUND"
            )

    asyncio.run(scenario())
