from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
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
    ConversationMemoryAdminError,
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


def _production_adapter(provider, data_root: Path, *, user_id: str = "local-user"):
    return Mem0ConversationMemoryAdapter(
        provider, Mem0Config(enabled=True, data_root=data_root, user_id=user_id,
                             llm_base_url="http://fixture.invalid/v1", llm_model="fixture-model"),
    )


async def _clear(client, request_id: str, reason: str):
    return await client.post(
        "/toy/companion/memory/clear",
        json={"request_id": request_id, "reason": reason, "confirmed": True},
        headers={"Origin": TRUSTED_ORIGIN, CONFIRM_HEADER: CONFIRM_VALUE},
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


def test_public_mem0_list_failure_fails_closed_for_clear_and_status(
    tmp_path: Path,
) -> None:
    class UnavailableMem0Fixture(ProductionMem0Fixture):
        def get_all(self, **kwargs):
            del kwargs
            raise RuntimeError("synthetic provider failure")

    async def scenario() -> None:
        adapter = _production_adapter(UnavailableMem0Fixture(), tmp_path / "memory" / "mem0")
        runtime = create_original_client_server_runtime(
            _fallback,
            memory_admin=ConversationMemoryAdminService(
                adapter, tmp_path / "memory" / "admin.sqlite3"
            ),
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(runtime.app)) as client:
            clear = await _clear(client, "request.memory.provider-failure.1", "synthetic user confirmation")
            assert clear.status == 503
            assert (await clear.json())["status"] == "UNAVAILABLE"
            status = await client.get(
                "/toy/companion/status",
                headers={"Origin": TRUSTED_ORIGIN},
            )
            assert status.status == 200
            payload = await status.json()
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
        adapter = _production_adapter(provider, tmp_path / "memory" / "mem0")
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
            failed = await _clear(client, "request.memory.pending.original", "synthetic confirmation")
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
            recovered = await _clear(client, "request.memory.pending.new", "synthetic confirmation retry")
            assert recovered.status == 200
            assert (await recovered.json())["status"] == "NOOP"
            healthy = await client.get(
                "/toy/companion/status", headers={"Origin": TRUSTED_ORIGIN}
            )
            assert (await healthy.json())["status"] == "READY"
        assert restarted_admin.run_write(lambda: "synthetic write") == "synthetic write"

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


def test_public_clear_rejects_impossible_pending_audit_shapes_before_delete(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        for name, pending_rows in (
            ("invalid", (("request.memory.pending.invalid", ("invalid memory id",), 0),)),
            ("duplicate", (("request.memory.pending.duplicate", ("memory.pending.one", "memory.pending.one"), 0),)),
            ("affected", (("request.memory.pending.affected", ("memory.pending.one",), 2),)),
            (
                "multiple",
                (
                    ("request.memory.pending.one", ("memory.pending.one",), 0),
                    ("request.memory.pending.two", ("memory.pending.two",), 0),
                ),
            ),
        ):
            provider = ProductionMem0Fixture()
            provider.rows.extend(
                {
                    "id": memory_id,
                    "memory": "synthetic pending memory",
                    "user_id": "local-user",
                    "agent_id": "linli",
                    "metadata": {
                        "source_id": f"reply:synthetic:{name}:{memory_id}",
                        "domain": "conversation_memory",
                    },
                }
                for _, memory_ids, _ in pending_rows
                for memory_id in memory_ids
            )
            adapter = _production_adapter(provider, tmp_path / name / "mem0")
            audit = tmp_path / name / "admin.sqlite3"
            admin = ConversationMemoryAdminService(adapter, audit)
            reason = "synthetic confirmation"
            fingerprint = hashlib.sha256(
                json.dumps(
                    {"operation": "clear", "payload": {"reason": reason}},
                    ensure_ascii=True, separators=(",", ":"), sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            with sqlite3.connect(audit) as connection:
                for request_id, memory_ids, affected_count in pending_rows:
                    connection.execute(
                        "INSERT INTO memory_admin_operations "
                        "(user_id,request_id,operation,payload_fingerprint,target_memory_id,"
                        "target_memory_ids,replacement_memory_id,replacement_source_id,status,"
                        "affected_count,reason,created_at,updated_at) "
                        "VALUES (?,?,'clear',?,NULL,?,NULL,NULL,'pending_clear',?,?,?,?)",
                        (
                            "local-user", request_id, fingerprint,
                            json.dumps(memory_ids, separators=(",", ":")), affected_count,
                            reason, "2026-08-26T00:00:00+00:00", "2026-08-26T00:00:00+00:00",
                        ),
                    )
            runtime = create_original_client_server_runtime(
                _fallback, memory_admin=admin, trusted_origins=(TRUSTED_ORIGIN,)
            )
            async with TestClient(TestServer(runtime.app)) as client:
                response = await _clear(client, f"request.memory.{name}.retry", reason)
                assert response.status == 503
                assert (await response.json())["error_code"] == "MEMORY_ADMIN_AUDIT_UNAVAILABLE"
                health = await client.get("/toy/companion/status", headers={"Origin": TRUSTED_ORIGIN})
                assert (await health.json())["status"] == "UNAVAILABLE"
            assert len(provider.rows) == sum(len(ids) for _, ids, _ in pending_rows)
            with pytest.raises(ConversationMemoryAdminError, match="MEMORY_ADMIN_AUDIT_UNAVAILABLE"):
                admin.run_write(lambda: pytest.fail("corrupt pending clear must block writes"))

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
                *[
                    {
                        "id": f"memory.pre-head-batch.{index}",
                        "memory": "synthetic batched memory",
                        "user_id": "User-A",
                        "agent_id": "linli",
                        "metadata": {
                            "source_id": f"reply:synthetic:batch:{index}",
                            "domain": "conversation_memory",
                        },
                    }
                    for index in range(1000)
                ],
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
        adapter = _production_adapter(provider, tmp_path / "memory" / "mem0", user_id="User-A")
        assert adapter.remember_exchange(
            user_message="synthetic user message",
            assistant_message="synthetic canonical reply",
            occurred_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
            source_id="reply:synthetic:case-normalization",
            user_id="User-A",
        ).status.value == "written"
        runtime = create_original_client_server_runtime(
            _fallback,
            memory_admin=ConversationMemoryAdminService(
                adapter, tmp_path / "memory" / "admin.sqlite3", user_id="user-a"
            ),
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(runtime.app)) as client:
            response = await _clear(client, "request.memory.pre-head-case.1", "synthetic upgrade confirmation")
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
