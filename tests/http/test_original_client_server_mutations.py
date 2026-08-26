from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
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
    ConversationMemoryAdminService,
    MemoryAdminMutationResult,
    MemoryAdminMutationStatus,
    MemoryAdminStatus,
)
from conversation_memory_port import NullConversationMemoryPort
from mem0_memory import Mem0Config, Mem0ConversationMemoryAdapter
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
        self.cleared: list[tuple[str, str, bool]] = []

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

    def clear(
        self,
        *,
        request_id: str,
        reason: str,
        confirmed: bool,
    ) -> MemoryAdminMutationResult:
        self.cleared.append((request_id, reason, confirmed))
        return MemoryAdminMutationResult(
            MemoryAdminMutationStatus.APPLIED,
            request_id,
            "clear",
            affected_count=2,
        )


class ProductionMem0Fixture:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.counter = 0

    def get_all(self, **kwargs):
        filters = kwargs["filters"]
        assert isinstance(filters, dict)
        return {
            "results": [
                row
                for row in self.rows
                if row["user_id"] == filters["user_id"]
                and row["agent_id"] == filters["agent_id"]
                and row["metadata"]["domain"] == filters["domain"]
                and (
                    "source_id" not in filters
                    or row["metadata"]["source_id"] == filters["source_id"]
                )
            ][: kwargs["top_k"]]
        }

    def add(self, messages, **kwargs):
        self.counter += 1
        memory_id = f"memory.production.{self.counter}"
        self.rows.append(
            {
                "id": memory_id,
                "memory": "synthetic production memory",
                "user_id": kwargs["user_id"],
                "agent_id": kwargs["agent_id"],
                "metadata": dict(kwargs["metadata"]),
            }
        )
        return {"results": [{"id": memory_id, "memory": "synthetic production memory", "event": "ADD"}]}

    def delete(self, memory_id):
        self.rows[:] = [row for row in self.rows if row["id"] != memory_id]
        return {"message": "Memory deleted successfully!"}

    def search(self, query, **kwargs):
        del query
        return self.get_all(**kwargs)

    def delete_all(self, **kwargs):
        raise AssertionError("domain-unscoped delete_all must not be used")


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

            cleared = await client.post(
                "/toy/companion/memory/clear",
                json={
                    "request_id": "request.memory.clear.1",
                    "reason": "用户明确清空当前长期记忆。",
                    "confirmed": True,
                },
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    CONFIRM_HEADER: CONFIRM_VALUE,
                },
            )
            assert cleared.status == 200
            assert (await cleared.json())["affected_count"] == 2

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
        assert memory.cleared == [
            (
                "request.memory.clear.1",
                "用户明确清空当前长期记忆。",
                True,
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


def test_production_mem0_write_then_public_clear_uses_the_same_normalized_user(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = ProductionMem0Fixture()
        adapter = Mem0ConversationMemoryAdapter(
            provider,
            Mem0Config(
                enabled=True,
                data_root=tmp_path / "memory" / "mem0",
                user_id="User-A",
                llm_base_url="http://fixture.invalid/v1",
                llm_model="fixture-model",
            ),
        )
        written = adapter.remember_exchange(
            user_message="synthetic user message",
            assistant_message="synthetic canonical reply",
            occurred_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
            source_id="reply:synthetic:case-normalization",
            user_id="User-A",
        )
        assert written.status.value == "written"
        runtime = create_original_client_server_runtime(
            _fallback,
            memory_admin=ConversationMemoryAdminService(
                adapter, tmp_path / "memory" / "admin.sqlite3", user_id="user-a"
            ),
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(runtime.app)) as client:
            response = await client.post(
                "/toy/companion/memory/clear",
                json={
                    "request_id": "request.memory.case-clear.1",
                    "reason": "synthetic user confirmation",
                    "confirmed": True,
                },
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    CONFIRM_HEADER: CONFIRM_VALUE,
                },
            )
            assert response.status == 200
            assert (await response.json())["status"] == "APPLIED"
        assert provider.rows == []

    asyncio.run(scenario())


def test_public_clear_does_not_turn_an_unavailable_mem0_list_into_noop(
    tmp_path: Path,
) -> None:
    class UnavailableMem0Fixture(ProductionMem0Fixture):
        def get_all(self, **kwargs):
            del kwargs
            raise RuntimeError("synthetic provider failure")

    async def scenario() -> None:
        adapter = Mem0ConversationMemoryAdapter(
            UnavailableMem0Fixture(),
            Mem0Config(
                enabled=True,
                data_root=tmp_path / "memory" / "mem0",
                llm_base_url="http://fixture.invalid/v1",
                llm_model="fixture-model",
            ),
        )
        runtime = create_original_client_server_runtime(
            _fallback,
            memory_admin=ConversationMemoryAdminService(
                adapter, tmp_path / "memory" / "admin.sqlite3"
            ),
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(runtime.app)) as client:
            response = await client.post(
                "/toy/companion/memory/clear",
                json={
                    "request_id": "request.memory.provider-failure.1",
                    "reason": "synthetic user confirmation",
                    "confirmed": True,
                },
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    CONFIRM_HEADER: CONFIRM_VALUE,
                },
            )
            assert response.status == 503
            assert (await response.json())["status"] == "UNAVAILABLE"

    asyncio.run(scenario())


def test_public_status_fails_closed_for_a_production_mem0_list_failure(
    tmp_path: Path,
) -> None:
    class UnavailableMem0Fixture(ProductionMem0Fixture):
        def get_all(self, **kwargs):
            del kwargs
            raise RuntimeError("synthetic provider failure")

    async def scenario() -> None:
        adapter = Mem0ConversationMemoryAdapter(
            UnavailableMem0Fixture(),
            Mem0Config(
                enabled=True,
                data_root=tmp_path / "memory" / "mem0",
                llm_base_url="http://fixture.invalid/v1",
                llm_model="fixture-model",
            ),
        )
        runtime = create_original_client_server_runtime(
            _fallback,
            memory_admin=ConversationMemoryAdminService(
                adapter, tmp_path / "memory" / "admin.sqlite3"
            ),
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(runtime.app)) as client:
            response = await client.get(
                "/toy/companion/status",
                headers={"Origin": TRUSTED_ORIGIN},
            )
            assert response.status == 200
            payload = await response.json()
            assert payload["status"] == "UNAVAILABLE"
            assert payload["capabilities"]["memory"] == {
                "state": "unavailable",
                "reason_code": "MEM0_LIST_FAILED",
            }

    asyncio.run(scenario())


def test_new_public_clear_request_recovers_a_pending_clear_after_restart(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = ProductionMem0Fixture()
        adapter = Mem0ConversationMemoryAdapter(
            provider,
            Mem0Config(
                enabled=True,
                data_root=tmp_path / "memory" / "mem0",
                llm_base_url="http://fixture.invalid/v1",
                llm_model="fixture-model",
            ),
        )
        assert adapter.remember_exchange(
            user_message="synthetic user message",
            assistant_message="synthetic canonical reply",
            occurred_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
            source_id="reply:synthetic:pending-recovery",
            user_id="local-user",
        ).status.value == "written"
        audit = tmp_path / "memory" / "admin.sqlite3"
        first_admin = ConversationMemoryAdminService(adapter, audit)
        with sqlite3.connect(audit) as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_clear_terminal_audit
                BEFORE INSERT ON memory_admin_operations
                WHEN NEW.operation = 'clear' AND NEW.status = 'completed'
                BEGIN SELECT RAISE(FAIL, 'synthetic terminal audit failure'); END
                """
            )
        first = create_original_client_server_runtime(
            _fallback,
            memory_admin=first_admin,
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(first.app)) as client:
            failed = await client.post(
                "/toy/companion/memory/clear",
                json={
                    "request_id": "request.memory.pending.original",
                    "reason": "synthetic confirmation",
                    "confirmed": True,
                },
                headers={"Origin": TRUSTED_ORIGIN, CONFIRM_HEADER: CONFIRM_VALUE},
            )
            assert failed.status == 503

        restarted_admin = ConversationMemoryAdminService(adapter, audit)
        restarted = create_original_client_server_runtime(
            _fallback,
            memory_admin=restarted_admin,
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        with sqlite3.connect(audit) as connection:
            connection.execute("DROP TRIGGER fail_clear_terminal_audit")
        async with TestClient(TestServer(restarted.app)) as client:
            pending = await client.get(
                "/toy/companion/status", headers={"Origin": TRUSTED_ORIGIN}
            )
            assert (await pending.json())["status"] == "UNAVAILABLE"
            recovered = await client.post(
                "/toy/companion/memory/clear",
                json={
                    "request_id": "request.memory.pending.new",
                    "reason": "synthetic confirmation retry",
                    "confirmed": True,
                },
                headers={"Origin": TRUSTED_ORIGIN, CONFIRM_HEADER: CONFIRM_VALUE},
            )
            assert recovered.status == 200
            assert (await recovered.json())["status"] == "NOOP"
            healthy = await client.get(
                "/toy/companion/status", headers={"Origin": TRUSTED_ORIGIN}
            )
            assert (await healthy.json())["status"] == "READY"
        assert restarted_admin.run_write(lambda: "synthetic write") == "synthetic write"

    asyncio.run(scenario())


def test_public_clear_maps_corrupt_pending_intent_to_auditable_unavailable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        adapter = Mem0ConversationMemoryAdapter(
            ProductionMem0Fixture(),
            Mem0Config(
                enabled=True,
                data_root=tmp_path / "memory" / "mem0",
                llm_base_url="http://fixture.invalid/v1",
                llm_model="fixture-model",
            ),
        )
        audit = tmp_path / "memory" / "admin.sqlite3"
        admin = ConversationMemoryAdminService(adapter, audit)
        with sqlite3.connect(audit) as connection:
            connection.execute(
                """
                INSERT INTO memory_admin_operations (
                    user_id, request_id, operation, payload_fingerprint,
                    target_memory_id, target_memory_ids, replacement_memory_id,
                    replacement_source_id, status, affected_count, reason,
                    created_at, updated_at
                ) VALUES (?, ?, 'clear', ?, NULL, ?, NULL, NULL, 'pending_clear', 0, ?, ?, ?)
                """,
                (
                    "local-user",
                    "request.memory.corrupt.pending",
                    "synthetic-fingerprint",
                    '["invalid memory id"]',
                    "synthetic confirmation",
                    "2026-08-26T00:00:00+00:00",
                    "2026-08-26T00:00:00+00:00",
                ),
            )
        runtime = create_original_client_server_runtime(
            _fallback,
            memory_admin=admin,
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(runtime.app)) as client:
            response = await client.post(
                "/toy/companion/memory/clear",
                json={
                    "request_id": "request.memory.corrupt.retry",
                    "reason": "synthetic confirmation retry",
                    "confirmed": True,
                },
                headers={"Origin": TRUSTED_ORIGIN, CONFIRM_HEADER: CONFIRM_VALUE},
            )
            payload = await response.json()
            assert response.status == 503
            assert payload["error_code"] == "MEMORY_ADMIN_AUDIT_UNAVAILABLE"
            assert "IDENTIFIER" not in payload["error_code"]

    asyncio.run(scenario())


def test_public_clear_rejects_duplicate_pending_ids_before_any_delete(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = ProductionMem0Fixture()
        provider.rows.append(
            {
                "id": "memory.corrupt.duplicate",
                "memory": "synthetic pending memory",
                "user_id": "local-user",
                "agent_id": "linli",
                "metadata": {
                    "source_id": "reply:synthetic:corrupt-pending",
                    "domain": "conversation_memory",
                },
            }
        )
        adapter = Mem0ConversationMemoryAdapter(
            provider,
            Mem0Config(
                enabled=True,
                data_root=tmp_path / "memory" / "mem0",
                llm_base_url="http://fixture.invalid/v1",
                llm_model="fixture-model",
            ),
        )
        audit = tmp_path / "memory" / "admin.sqlite3"
        admin = ConversationMemoryAdminService(adapter, audit)
        reason = "synthetic confirmation"
        fingerprint = hashlib.sha256(
            json.dumps(
                {"operation": "clear", "payload": {"reason": reason}},
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        with sqlite3.connect(audit) as connection:
            connection.execute(
                """
                INSERT INTO memory_admin_operations (
                    user_id, request_id, operation, payload_fingerprint,
                    target_memory_id, target_memory_ids, replacement_memory_id,
                    replacement_source_id, status, affected_count, reason,
                    created_at, updated_at
                ) VALUES (?, ?, 'clear', ?, NULL, ?, NULL, NULL, 'pending_clear', 0, ?, ?, ?)
                """,
                (
                    "local-user",
                    "request.memory.duplicate.pending",
                    fingerprint,
                    '["memory.corrupt.duplicate","memory.corrupt.duplicate"]',
                    reason,
                    "2026-08-26T00:00:00+00:00",
                    "2026-08-26T00:00:00+00:00",
                ),
            )
        runtime = create_original_client_server_runtime(
            _fallback,
            memory_admin=admin,
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(runtime.app)) as client:
            response = await client.post(
                "/toy/companion/memory/clear",
                json={
                    "request_id": "request.memory.duplicate.retry",
                    "reason": "synthetic confirmation retry",
                    "confirmed": True,
                },
                headers={"Origin": TRUSTED_ORIGIN, CONFIRM_HEADER: CONFIRM_VALUE},
            )
            payload = await response.json()
            assert response.status == 503
            assert payload["error_code"] == "MEMORY_ADMIN_AUDIT_UNAVAILABLE"
            assert [row["id"] for row in provider.rows] == ["memory.corrupt.duplicate"]
            health = await client.get(
                "/toy/companion/status", headers={"Origin": TRUSTED_ORIGIN}
            )
            health_payload = await health.json()
            assert health_payload["status"] == "UNAVAILABLE"
            assert health_payload["capabilities"]["memory"] == {
                "state": "unavailable",
                "reason_code": "MEMORY_ADMIN_CLEAR_PENDING",
            }

    asyncio.run(scenario())


def test_public_clear_advertises_an_invalid_direct_backend_result(
) -> None:
    class InvalidResultMemoryAdmin(MemoryAdminFixture):
        def clear(self, **kwargs):
            del kwargs
            return object()

    async def scenario() -> None:
        runtime = create_original_client_server_runtime(
            _fallback,
            memory_admin=InvalidResultMemoryAdmin(),
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(runtime.app)) as client:
            response = await client.post(
                "/toy/companion/memory/clear",
                json={
                    "request_id": "request.memory.invalid-result",
                    "reason": "synthetic confirmation",
                    "confirmed": True,
                },
                headers={"Origin": TRUSTED_ORIGIN, CONFIRM_HEADER: CONFIRM_VALUE},
            )
            assert response.status == 503
            assert (await response.json())["error_code"] == "MEMORY_MUTATION_RESULT_INVALID"

    asyncio.run(scenario())


def test_head_public_clear_preserves_other_domains_while_clearing_pre_head_case(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = ProductionMem0Fixture()
        provider.rows.extend(
            [
                {
                    "id": "memory.pre-head-case",
                    "memory": "synthetic pre-head memory",
                    "user_id": "User-A",
                    "agent_id": "linli",
                    "metadata": {
                        "source_id": "reply:synthetic:pre-head",
                        "domain": "conversation_memory",
                    },
                },
                {
                    "id": "memory.pre-head-other-domain",
                    "memory": "synthetic other domain",
                    "user_id": "User-A",
                    "agent_id": "linli",
                    "metadata": {
                        "source_id": "other:synthetic:pre-head",
                        "domain": "other_domain",
                    },
                },
                {
                    "id": "memory.other-user",
                    "memory": "synthetic other user",
                    "user_id": "user-b",
                    "agent_id": "linli",
                    "metadata": {
                        "source_id": "reply:synthetic:other-user",
                        "domain": "conversation_memory",
                    },
                },
            ]
        )
        adapter = Mem0ConversationMemoryAdapter(
            provider,
            Mem0Config(
                enabled=True,
                data_root=tmp_path / "memory" / "mem0",
                user_id="User-A",
                llm_base_url="http://fixture.invalid/v1",
                llm_model="fixture-model",
            ),
        )
        runtime = create_original_client_server_runtime(
            _fallback,
            memory_admin=ConversationMemoryAdminService(
                adapter, tmp_path / "memory" / "admin.sqlite3", user_id="user-a"
            ),
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(runtime.app)) as client:
            response = await client.post(
                "/toy/companion/memory/clear",
                json={
                    "request_id": "request.memory.pre-head-case.1",
                    "reason": "synthetic upgrade confirmation",
                    "confirmed": True,
                },
                headers={"Origin": TRUSTED_ORIGIN, CONFIRM_HEADER: CONFIRM_VALUE},
            )
            assert response.status == 200
            assert (await response.json())["status"] == "APPLIED"
        assert [row["id"] for row in provider.rows] == [
            "memory.pre-head-other-domain",
            "memory.other-user",
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
