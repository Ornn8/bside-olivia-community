"""B02 route, error, retry, empty-data, capability, and privacy coverage."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _assert_memory_outbox_runtime_schema(payload: dict[str, object]) -> None:
    from jsonschema import Draft202012Validator

    schema = json.loads((ROOT / "contracts" / "memory_outbox_runtime.schema.json").read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(schema).iter_errors(payload))


@pytest.fixture(autouse=True)
def reset_local_store():
    import local_server

    local_server.store.letters.clear()
    local_server.store.legacy_letters.clear()
    local_server.store.midi_jobs.clear()
    local_server.store.request_keys.clear()
    yield
    local_server.store.letters.clear()
    local_server.store.legacy_letters.clear()
    local_server.store.midi_jobs.clear()
    local_server.store.request_keys.clear()


def _post_current_letter(local_server, content: str) -> tuple[int, dict]:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def exercise() -> tuple[int, dict]:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app, access_log=None)) as client:
            response = await client.post(
                "/toy/letter/send",
                json={"content": content},
            )
            return response.status, await response.json()

    return asyncio.run(exercise())


def _get_mailbox_responses(
    local_server, detail_id: str = "synthetic-missing"
) -> list[tuple[int, dict]]:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def exercise() -> list[tuple[int, dict]]:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app, access_log=None)) as client:
            responses = []
            for path, params in (
                ("/toy/letter/list", {}),
                ("/toy/letter/unread_count", {}),
                ("/toy/letter/detail", {"letter_id": detail_id}),
                ("/toy/letter/list", {"scope": "legacy"}),
            ):
                response = await client.get(path, params=params)
                responses.append((response.status, await response.json()))
            return responses

    return asyncio.run(exercise())


def test_core_health_is_versioned_and_reports_unavailable_optional_capabilities() -> None:
    import local_server

    result = asyncio.run(local_server.route("GET", "/health", {}, {"profile": "core"}))
    data = result["data"]

    assert result["code"] == 0
    assert data["status"] == "HEALTHY"
    assert data["contract_version"] == "b02.v1"
    assert data["schema_version"] == 1
    assert data["backend_id"] == "legacy"
    assert data["privacy"]["logs_include_request_body"] is False
    assert data["privacy"]["logs_include_query_values"] is False
    for capability in ("native.websocket", "native.asr", "native.tts", "native.live"):
        assert data["capabilities"][capability]["status"] == "unavailable"


def test_http_startup_exposes_core_health_while_mem0_initializes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from conversation_memory_port import (
        ConversationMemoryStatus,
        NullConversationMemoryPort,
        UnavailableConversationMemoryPort,
    )
    from conversation_memory_runtime import ConversationMemoryRuntimeStatus
    from mem0_memory import DeferredConversationMemoryAdapter, Mem0Config

    import local_server
    entered = threading.Event()
    release = threading.Event()
    generated: list[str] = []
    runtime_starts: list[int] = []
    runtime_state = ["unavailable"]
    factory_calls: list[str] = []
    class ReadyMemory(NullConversationMemoryPort):
        enabled = True

        def status(self) -> ConversationMemoryStatus:
            return ConversationMemoryStatus("available", True, "mem0", "qdrant-local")

    def blocked_factory():
        factory_calls.append("old")
        entered.set()
        assert release.wait(2)
        return ReadyMemory()

    memory = DeferredConversationMemoryAdapter(Mem0Config(enabled=True, data_root=tmp_path), blocked_factory)
    monkeypatch.setattr(local_server, "conversation_memory_adapter", memory)
    monkeypatch.setattr(local_server.letters_adapter.memory_prompt_builder, "conversation_runtime_status", None)
    async def record_generation(_letter_id: str, content: str, **_kwargs) -> bool:
        generated.append(content)
        letter = next(item for item in local_server.store.letters if item["letter_id"] == _letter_id)
        letter["letter_status"] = "COMPLETED"
        return True

    monkeypatch.setattr(local_server, "generate_reply", record_generation)
    def runtime_status(*_args, **_kwargs):
        runtime_starts.append(1)
        state = "unavailable" if len(runtime_starts) == 1 else "available"
        runtime_state[0] = state
        return ConversationMemoryRuntimeStatus(state, True, "mem0-outbox", state == "available")
    monkeypatch.setattr(
        local_server,
        "ensure_conversation_memory_runtime",
        runtime_status,
    )
    def current_runtime_status() -> ConversationMemoryRuntimeStatus:
        state = runtime_state[0]
        return ConversationMemoryRuntimeStatus(
            state, True, "mem0-outbox", state == "available"
        )

    monkeypatch.setattr(local_server, "conversation_memory_runtime_status", current_runtime_status)
    monkeypatch.setattr(local_server, "conversation_memory_reply_readiness_status", current_runtime_status)
    history_calls: list[str] = []
    monkeypatch.setattr(local_server, "collect_default_official_text_replies", lambda: history_calls.append("collector"))
    monkeypatch.setattr(local_server, "_legacy_import_adapter", lambda: history_calls.append("archive"))

    async def exercise() -> tuple:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        local_server.install_reply_task_lifecycle(app)
        started_at = asyncio.get_running_loop().time()
        async with TestClient(TestServer(app, access_log=None)) as client:
            response = await client.get("/health", params={"profile": "core"})
            health = await response.json()
            health_elapsed = asyncio.get_running_loop().time() - started_at
            imported = await client.post("/toy/letter/legacy/official-import", json={}, headers={"X-Olivia-Companion-Action": "confirmed"})
            await client.post("/toy/letter/send", json={"content": "synthetic letter"})
            assert entered.wait(0.2)
            generated_before_ready = len(generated)
            await client.post("/toy/companion/memory/retry", json={}, headers={"X-Olivia-Companion-Action": "confirmed"})
            latest = DeferredConversationMemoryAdapter(Mem0Config(enabled=True, data_root=tmp_path), lambda: (factory_calls.append("latest"), ReadyMemory())[1])
            assert memory.reconfigure_from(latest) and not memory.start_initialization()
            assert factory_calls == ["old"]
            release.set()
            await asyncio.sleep(0.05)
            generated_before_runtime_retry = len(generated)
            retry = await client.post("/toy/companion/memory/retry", json={}, headers={
                "X-Olivia-Companion-Action": "confirmed"
            })
            deadline = asyncio.get_running_loop().time() + 1
            while not generated and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            runtime_state[0] = "unavailable"
            await client.post("/toy/letter/send", json={"content": "letter during runtime failure"})
            await asyncio.sleep(0.05)
            generated_during_runtime_failure = len(generated)
            runtime_state[0] = "available"
            deadline = asyncio.get_running_loop().time() + 2
            while len(generated) < 2 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            imported_payload, retry_payload = await imported.json(), await retry.json()
        closed_after_first = memory.closed
        restarted = web.Application()
        restarted.router.add_route("*", "/{tail:.*}", local_server.handler)
        local_server.install_reply_task_lifecycle(restarted)
        async with TestClient(TestServer(restarted, access_log=None)):
            pass
        failure = DeferredConversationMemoryAdapter(Mem0Config(enabled=True, data_root=tmp_path), lambda: UnavailableConversationMemoryPort("MEM0_IMPORT_FAILED"))
        assert memory.reconfigure_from(failure) and memory.start_initialization()
        await asyncio.sleep(0.05)
        failure_reason = memory.status().reason_code
        assert memory.reconfigure_from(DeferredConversationMemoryAdapter(Mem0Config(enabled=True, data_root=tmp_path), ReadyMemory)) and memory.start_initialization()
        await asyncio.sleep(0.05)
        recovered = memory.status().status
        memory.close()
        return health, health_elapsed, generated_before_ready, generated_before_runtime_retry, generated_during_runtime_failure, imported_payload, retry_payload, closed_after_first, failure_reason, recovered

    try:
        result, elapsed, generated_before_ready, generated_before_retry, generated_during_runtime_failure, imported, retry, closed_after_first, failure_reason, recovered = asyncio.run(exercise())
        assert elapsed < 1
        assert result["data"]["status"] == "HEALTHY"
        assert result["data"]["providers"]["memory"]["conversation"]["reason_code"] == "MEM0_INITIALIZING"
        assert generated_before_ready == 0
        assert generated_before_retry == 0
        assert generated_during_runtime_failure == 1
        assert generated == ["synthetic letter", "letter during runtime failure"]
        assert history_calls == [] and imported["message"] == "OFFICIAL_HISTORY_MEMORY_UNAVAILABLE"
        assert runtime_starts == [1, 1, 1]
        assert retry["data"]["status"] == "AVAILABLE"
        assert closed_after_first and memory.closed and local_server._start_ready_conversation_memory_runtime() is None
        assert failure_reason == "MEM0_IMPORT_FAILED" and recovered == "available"
        assert factory_calls == ["old", "latest"]
    finally:
        release.set()


def test_memory_readiness_deadline_fails_pending_letter_before_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    letter = {
        "letter_id": "memory-deadline-letter",
        "content": "synthetic memory deadline input",
        "reply_text": "",
        "reply_mode": "text_letter",
        "letter_status": "PENDING",
    }
    local_server.store.letters[:] = [letter]
    generated: list[str] = []
    persisted: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        local_server,
        "_conversation_memory_ready_for_reply",
        lambda: False,
    )

    async def record_generation(*_args, **_kwargs) -> bool:
        generated.append("called")
        return True

    monkeypatch.setattr(local_server, "_run_reply_job", record_generation)
    monkeypatch.setattr(
        local_server,
        "_persist_store_state",
        lambda: persisted.append(
            (letter["letter_status"], letter.get("error_code"))
        ),
    )

    completed = asyncio.run(
        local_server._run_reply_when_memory_ready(
            letter["letter_id"],
            letter["content"],
            idempotency_key="memory-deadline-key",
            ready_timeout_seconds=0.01,
        )
    )

    assert completed is False
    assert generated == []
    assert persisted == [("FAILED", "MEMORY_UNAVAILABLE")]
    assert letter["letter_status"] == "FAILED"
    assert letter["error_code"] == "MEMORY_UNAVAILABLE"
    assert letter["reply_text"] == ""
    assert local_server._active_undelivered_letter() is None
    assert local_server._send_result_for_letter(letter) == {
        "code": 503,
        "message": "MEMORY_UNAVAILABLE",
        "data": {
            "letter_id": "memory-deadline-letter",
            "status": "FAILED",
            "error_code": "MEMORY_UNAVAILABLE",
            "retryable": True,
        },
    }

    second = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            {"content": "synthetic next letter"},
            {},
            defer_reply=True,
        )
    )
    assert second["code"] == 0
    assert second["data"]["letter_id"] != letter["letter_id"]


@pytest.mark.parametrize(
    ("bootstrap", "runtime_state", "expected"),
    [
        (("disabled", "none"), ("disabled", False, False, None), True),
        (("disabled", "mem0-outbox"), ("disabled", False, False, None), False),
        (("unavailable", "mem0-outbox"), ("disabled", False, False, None), False),
        (("available", "mem0-outbox"), ("available", True, True, None), True),
        (("available", "mem0-outbox"), ("available", True, False, None), False),
        (("available", "mem0-outbox"), ("degraded", True, True, "MEMORY_ADMIN_PAUSED"), True),
        (("available", "mem0-outbox"), ("degraded", True, False, "MEMORY_ADMIN_PAUSED"), False),
        (("available", "mem0-outbox"), ("degraded", True, True, "MEMORY_OUTBOX_DELIVERY_FAILED"), False),
        (("available", "mem0-outbox"), ("unavailable", True, True, "MEMORY_OUTBOX_STORAGE_UNAVAILABLE"), False),
    ],
)
def test_memory_readiness_uses_provider_free_runtime_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    bootstrap: tuple[str, str],
    runtime_state: tuple[str, bool, bool, str | None],
    expected: bool,
) -> None:
    import local_server
    from conversation_memory_runtime import ConversationMemoryRuntimeStatus

    bootstrap_status, bootstrap_provider = bootstrap
    runtime_status, runtime_enabled, runtime_worker, runtime_reason = runtime_state

    class ProviderStatusMustNotRun:
        def status(self):
            raise AssertionError("reply readiness must not call the memory provider")

    runtime = ConversationMemoryRuntimeStatus(
        status=runtime_status,
        enabled=runtime_enabled,
        provider="mem0-outbox" if runtime_enabled else "none",
        worker_running=runtime_worker,
        reason_code=runtime_reason,
    )
    assert runtime.enabled is runtime_enabled
    assert runtime.worker_running is runtime_worker
    monkeypatch.setattr(
        local_server.letters_adapter.memory_prompt_builder,
        "conversation_runtime_status",
        {
            "status": bootstrap_status,
            "enabled": bootstrap_status != "disabled",
            "provider": bootstrap_provider,
            "worker_running": runtime_worker,
        },
    )
    monkeypatch.setattr(
        local_server,
        "conversation_memory_adapter",
        ProviderStatusMustNotRun(),
    )
    monkeypatch.setattr(
        local_server,
        "conversation_memory_reply_readiness_status",
        lambda: runtime,
        raising=False,
    )

    assert local_server._conversation_memory_ready_for_reply() is expected


def test_memory_readiness_deadline_does_not_reset_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    letter = {
        "letter_id": "memory-restart-deadline-letter",
        "content": "synthetic old pending memory input",
        "reply_text": "",
        "reply_mode": "text_letter",
        "letter_status": "PENDING",
        "created_at": int(time.time()) - 121,
    }
    local_server.store.letters[:] = [letter]
    generated: list[str] = []
    persisted: list[str] = []
    readiness_checks: list[str] = []

    def memory_ready() -> bool:
        readiness_checks.append("called")
        return True

    monkeypatch.setattr(
        local_server,
        "_conversation_memory_ready_for_reply",
        memory_ready,
    )

    async def record_generation(*_args, **_kwargs) -> bool:
        generated.append("called")
        return True

    monkeypatch.setattr(local_server, "_run_reply_job", record_generation)
    monkeypatch.setattr(
        local_server,
        "_persist_store_state",
        lambda: persisted.append(letter["letter_status"]),
    )

    async def exercise() -> bool:
        return await asyncio.wait_for(
            local_server._run_reply_when_memory_ready(
                letter["letter_id"],
                letter["content"],
                idempotency_key=None,
            ),
            timeout=0.05,
        )

    assert asyncio.run(exercise()) is False
    assert letter["letter_status"] == "FAILED"
    assert letter["error_code"] == "MEMORY_UNAVAILABLE"
    assert generated == []
    assert persisted == ["FAILED"]
    assert readiness_checks == []


def test_memory_readiness_recovery_dispatches_pending_letter_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    checks = 0
    generated: list[tuple[str, str, str | None]] = []

    def memory_ready() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 2

    async def record_generation(
        letter_id: str,
        content: str,
        *,
        idempotency_key: str | None,
    ) -> bool:
        generated.append((letter_id, content, idempotency_key))
        return True

    monkeypatch.setattr(
        local_server,
        "_conversation_memory_ready_for_reply",
        memory_ready,
    )
    monkeypatch.setattr(local_server, "_run_reply_job", record_generation)
    original_sleep = asyncio.sleep

    async def yield_once(_delay: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(local_server.asyncio, "sleep", yield_once)

    completed = asyncio.run(
        local_server._run_reply_when_memory_ready(
            "memory-recovered-letter",
            "synthetic recovered memory input",
            idempotency_key="memory-recovered-key",
            ready_timeout_seconds=0.5,
        )
    )

    assert completed is True
    assert generated == [
        (
            "memory-recovered-letter",
            "synthetic recovered memory input",
            "memory-recovered-key",
        )
    ]


def test_memory_readiness_waiter_cancellation_keeps_letter_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    letter = {
        "letter_id": "memory-cancelled-letter",
        "content": "synthetic cancelled memory wait",
        "reply_text": "",
        "reply_mode": "text_letter",
        "letter_status": "PENDING",
    }
    local_server.store.letters[:] = [letter]
    checks: list[bool] = []
    generated: list[str] = []
    persisted: list[str] = []

    def memory_ready() -> bool:
        checks.append(False)
        return False

    async def record_generation(*_args, **_kwargs) -> bool:
        generated.append("called")
        return True

    monkeypatch.setattr(
        local_server,
        "_conversation_memory_ready_for_reply",
        memory_ready,
    )
    monkeypatch.setattr(local_server, "_run_reply_job", record_generation)
    monkeypatch.setattr(
        local_server,
        "_persist_store_state",
        lambda: persisted.append("called"),
    )

    async def exercise() -> None:
        task = asyncio.create_task(
            local_server._run_reply_when_memory_ready(
                letter["letter_id"],
                letter["content"],
                idempotency_key=None,
                ready_timeout_seconds=10,
            )
        )
        while not checks:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert letter["letter_status"] == "PENDING"
    assert "error_code" not in letter
    assert generated == []
    assert persisted == []


@pytest.mark.parametrize(
    ("adapter_state", "runtime_state", "expected"),
    [("available", "degraded", ("DEGRADED", True)), ("disabled", None, ("DISABLED", False))],
)
def test_memory_retry_reports_runtime_degradation_and_disabled_state(monkeypatch: pytest.MonkeyPatch, adapter_state: str, runtime_state: str | None, expected: tuple[str, bool]) -> None:
    import local_server
    from conversation_memory_port import ConversationMemoryStatus
    from conversation_memory_runtime import ConversationMemoryRuntimeStatus
    class Adapter:
        def status(self):
            return ConversationMemoryStatus(adapter_state, adapter_state != "disabled", "mem0" if adapter_state != "disabled" else "none", "qdrant-local" if adapter_state != "disabled" else "none")
    monkeypatch.setattr(local_server, "conversation_memory_adapter", Adapter())
    monkeypatch.setattr(local_server, "_start_conversation_memory_initialization", lambda _loop: False)
    monkeypatch.setattr(local_server, "_start_ready_conversation_memory_runtime", lambda: None if runtime_state is None else ConversationMemoryRuntimeStatus(runtime_state, True, "mem0-outbox", False))
    result = asyncio.run(local_server.route("POST", "/toy/companion/memory/retry", {}, {}, companion_confirmed=True))
    assert (result["data"]["status"], result["data"]["retryable"]) == expected


def test_invalid_health_profile_is_a_stable_client_error() -> None:
    import local_server

    result = asyncio.run(local_server.route("GET", "/health", {}, {"profile": "unknown"}))

    assert result == {
        "code": 400,
        "message": "INVALID_PROFILE",
        "data": {"status": "FAILED", "error_code": "INVALID_PROFILE"},
    }


def test_sqlite_memory_health_is_json_serializable_and_keeps_domain_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server
    from conversation_memory_port import NullConversationMemoryPort

    class SQLiteArchive:
        def status(self):
            return {
                "status": "available",
                "enabled": True,
                "provider": "sqlite",
                "storage": "sqlite",
                "conversation_enabled": True,
                "network_called": False,
            }

    monkeypatch.setattr(local_server, "memory_adapter", SQLiteArchive())
    monkeypatch.setattr(
        local_server,
        "conversation_memory_adapter",
        NullConversationMemoryPort(),
    )

    result = asyncio.run(local_server.route("GET", "/health", {}, {"profile": "memory"}))

    json.dumps(result)
    memory = result["data"]["providers"]["memory"]
    assert memory["conversation"] == {
        "status": "available",
        "enabled": True,
        "provider": "sqlite",
        "storage": "sqlite",
        "conversation_enabled": True,
        "network_called": False,
    }
    assert result["data"]["capabilities"]["memory.legacy"]["status"] == "available"
    assert result["data"]["capabilities"]["memory.conversation"]["status"] == "available"


def test_memory_health_fails_closed_when_mem0_is_unavailable_but_archive_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server
    from conversation_memory_port import UnavailableConversationMemoryPort

    class ReadOnlyArchive:
        def status(self):
            return {
                "status": "available",
                "enabled": True,
                "provider": "sqlite",
                "storage": "sqlite",
                "conversation_enabled": False,
                "network_called": False,
            }

    monkeypatch.setattr(local_server, "memory_adapter", ReadOnlyArchive())
    monkeypatch.setattr(
        local_server,
        "conversation_memory_adapter",
        UnavailableConversationMemoryPort(
            "MEM0_TELEMETRY_STATE_UNAVAILABLE",
            config={"telemetry": "private-synthetic-telemetry-detail"},
        ),
    )

    result = asyncio.run(local_server.route("GET", "/health", {}, {"profile": "memory"}))

    assert result["data"]["status"] == "UNAVAILABLE"
    assert result["data"]["capabilities"]["memory.legacy"]["status"] == "available"
    conversation = result["data"]["capabilities"]["memory.conversation"]
    assert conversation["status"] == "unavailable"
    assert conversation["provider"] == "none"
    health = json.dumps(result)
    assert "MEM0_TELEMETRY_STATE_UNAVAILABLE" in health
    assert "private-synthetic-telemetry-detail" not in health


def test_memory_health_keeps_lifecycle_audit_failure_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server
    from conversation_memory_port import ConversationMemoryStatus
    from conversation_memory_runtime import ConversationMemoryRuntimeStatus

    class AvailableMem0:
        def status(self):
            return ConversationMemoryStatus(
                "available", True, "mem0", "qdrant-local", memory_count=0
            )

    class UnavailableLifecycle:
        def is_paused(self) -> bool:
            raise RuntimeError("synthetic schema incompatibility")

    memory = AvailableMem0()
    monkeypatch.setattr(local_server, "conversation_memory_adapter", memory)
    monkeypatch.setattr(
        local_server.letters_adapter.memory_prompt_builder,
        "conversation_memory",
        memory,
    )
    monkeypatch.setattr(
        local_server.letters_adapter.memory_prompt_builder,
        "memory_lifecycle",
        UnavailableLifecycle(),
    )
    monkeypatch.setattr(
        local_server.letters_adapter.memory_prompt_builder,
        "conversation_runtime_status",
        {
            "status": "degraded",
            "enabled": True,
            "provider": "mem0-outbox",
            "worker_running": True,
            "reason_code": "MEMORY_OUTBOX_DELIVERY_FAILED",
        },
    )
    monkeypatch.setattr(
        local_server,
        "conversation_memory_runtime_status",
        lambda: ConversationMemoryRuntimeStatus(
            "degraded",
            True,
            "mem0-outbox",
            True,
            reason_code="MEMORY_OUTBOX_DELIVERY_FAILED",
        ),
    )

    result = asyncio.run(local_server.route("GET", "/health", {}, {"profile": "memory"}))

    conversation = result["data"]["providers"]["memory"]["conversation"]
    assert result["data"]["status"] == "UNAVAILABLE"
    assert conversation["status"] == "unavailable"
    assert conversation["reason_code"] == "MEMORY_ADMIN_AUDIT_UNAVAILABLE"
    assert result["data"]["capabilities"]["memory.conversation"]["status"] == "unavailable"


def test_memory_health_reflects_degraded_canonical_delivery_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server
    from conversation_memory_port import ConversationMemoryStatus
    from conversation_memory_runtime import ConversationMemoryRuntimeStatus

    class ReadOnlyArchive:
        def status(self):
            return {
                "status": "available",
                "enabled": True,
                "provider": "sqlite",
                "storage": "sqlite",
                "conversation_enabled": False,
                "network_called": False,
            }

    class AvailableMem0:
        def status(self):
            return ConversationMemoryStatus(
                "available",
                True,
                "mem0",
                "qdrant-local",
                memory_count=0,
            )

    monkeypatch.setattr(local_server, "memory_adapter", ReadOnlyArchive())
    monkeypatch.setattr(local_server, "conversation_memory_adapter", AvailableMem0())
    monkeypatch.setattr(
        local_server.letters_adapter.memory_prompt_builder,
        "conversation_runtime_status",
        {
            "status": "available",
            "enabled": True,
            "provider": "mem0-outbox",
            "worker_running": True,
        },
    )
    for runtime, expected, provider, probe in (
        (
            ConversationMemoryRuntimeStatus(
                "degraded", True, "mem0-outbox", False,
                reason_code="MEMORY_OUTBOX_DELIVERY_FAILED",
                pending_count=1,
                attempt_count=1,
            ),
            "degraded",
            "mem0",
            "in-process",
        ),
        (
            ConversationMemoryRuntimeStatus(
                "unavailable", True, "mem0-outbox", True,
                reason_code="MEMORY_OUTBOX_STORAGE_UNAVAILABLE",
            ),
            "unavailable",
            "none",
            "not-run",
        ),
    ):
        monkeypatch.setattr(local_server, "conversation_memory_runtime_status", lambda: runtime, raising=False)
        result = asyncio.run(local_server.route("GET", "/health", {}, {"profile": "memory"}))
        conversation = result["data"]["providers"]["memory"]["conversation"]
        assert result["data"]["status"] == "UNAVAILABLE"
        assert conversation["status"] == expected
        _assert_memory_outbox_runtime_schema(conversation["runtime"])
        capability = result["data"]["capabilities"]["memory.conversation"]
        assert capability["status"] == expected
        assert capability["provider"] == provider
        assert capability["probe"] == probe


def test_public_memory_outbox_runtime_schema_matches_every_emitted_state() -> None:
    from jsonschema import Draft202012Validator
    schema = json.loads(
        (ROOT / "contracts" / "memory_outbox_runtime.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = Draft202012Validator(schema)
    base = {"terminal_count": 0, "pending_count": 0, "attempt_count": 0}
    valid = [
        {**base, "status": "disabled", "enabled": False, "provider": "none", "worker_running": False},
        {**base, "status": "disabled", "enabled": False, "provider": "mem0-outbox", "worker_running": False},
        {**base, "status": "unavailable", "enabled": False, "provider": "mem0", "worker_running": False, "reason_code": "MEM0_INITIALIZATION_FAILED"},
        {**base, "status": "unavailable", "enabled": False, "provider": "mem0-outbox", "worker_running": False, "reason_code": "MEMORY_OUTBOX_INITIALIZATION_FAILED"},
        {**base, "status": "unavailable", "enabled": False, "provider": "none", "worker_running": False, "reason_code": "MEMORY_OUTBOX_RUNTIME_UNAVAILABLE"},
        {**base, "status": "unavailable", "enabled": True, "provider": "mem0-outbox", "worker_running": True, "reason_code": "MEMORY_OUTBOX_STORAGE_UNAVAILABLE"},
        {**base, "status": "degraded", "enabled": True, "provider": "mem0", "worker_running": False, "reason_code": "MEMORY_OUTBOX_DATA_ROOT_NOT_CONFIGURED"},
        {**base, "status": "degraded", "enabled": True, "provider": "mem0-outbox", "worker_running": False, "reason_code": "MEMORY_OUTBOX_WORKER_NOT_RUNNING"},
        {**base, "status": "degraded", "enabled": True, "provider": "mem0-outbox", "worker_running": True, "reason_code": "MEMORY_OUTBOX_DELIVERY_FAILED"},
        {**base, "status": "available", "enabled": True, "provider": "mem0-outbox", "worker_running": True},
    ]
    for payload in valid:
        assert list(validator.iter_errors(payload)) == []
    contradictory = [
        {**base, "status": "available", "enabled": False, "provider": "none", "worker_running": False},
        {**base, "status": "disabled", "enabled": True, "provider": "mem0-outbox", "worker_running": True},
        {**base, "status": "unavailable", "enabled": False, "provider": "mem0", "worker_running": False},
        {**base, "status": "degraded", "enabled": True, "provider": "none", "worker_running": False, "reason_code": "MEM0_WRITE_FAILED"},
        {**base, "status": "available", "enabled": True, "provider": "mem0-outbox", "worker_running": True, "reason_code": "MEM0_WRITE_FAILED"},
        {**valid[3], "data_root": "must-not-be-public"},
        {**valid[3], "reason_code": "private config value"},
    ]
    for payload in contradictory:
        assert list(validator.iter_errors(payload))


def test_memory_health_uses_public_runtime_unavailable_reason_without_config_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server
    from conversation_memory_port import ConversationMemoryStatus
    from conversation_memory_runtime import ConversationMemoryRuntimeStatus
    class AvailableMem0:
        def status(self):
            return ConversationMemoryStatus("available", True, "mem0", "qdrant-local")
    monkeypatch.setattr(local_server, "conversation_memory_adapter", AvailableMem0())
    monkeypatch.setattr(
        local_server.letters_adapter.memory_prompt_builder,
        "conversation_runtime_status",
        {"status": "available", "enabled": True, "provider": "mem0-outbox", "worker_running": True},
    )
    monkeypatch.setattr(
        local_server,
        "conversation_memory_runtime_status",
        lambda: ConversationMemoryRuntimeStatus("disabled", False, "none", False),
    )
    result = asyncio.run(local_server.route("GET", "/health", {}, {"profile": "memory"}))
    runtime = result["data"]["providers"]["memory"]["conversation"]["runtime"]
    assert runtime["status"] == "unavailable"
    assert runtime["reason_code"] == "MEMORY_OUTBOX_RUNTIME_UNAVAILABLE"
    _assert_memory_outbox_runtime_schema(runtime)
    assert "root" not in json.dumps(runtime).casefold()
    assert "key" not in json.dumps(runtime).casefold()


def test_empty_letter_and_music_paths_are_explicitly_empty() -> None:
    import local_server

    letter_list = asyncio.run(local_server.route("GET", "/toy/letter/list", {}, {}))
    unread = asyncio.run(local_server.route("GET", "/toy/letter/unread_count", {}, {}))
    songs = asyncio.run(
        local_server.route("GET", "/toy/searchSongs", {}, {"style_type": "missing-style"})
    )

    assert letter_list["code"] == 0
    assert letter_list["data"]["list"] == []
    assert letter_list["data"]["source"] == "local-memory"
    assert unread["data"]["unread_count"] == 0
    assert songs["code"] == 0
    assert songs["data"]["list"] == []
    assert songs["data"]["source"] == "empty"


def test_letter_gateway_preserves_durable_request_id_to_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    seen_request_ids: list[str | None] = []

    class CapturingGateway:
        async def complete(self, _messages, *, request_id=None):
            seen_request_ids.append(request_id)
            return local_server.GatewayResponse(
                text="synthetic reply",
                request_id=request_id or "provider-generated-id",
                provider="mock",
                model="mock-model",
            )

    monkeypatch.setattr(local_server.letters_adapter, "gateway", CapturingGateway())
    durable_request_id = "letter-reply:synthetic-letter-id"

    response = asyncio.run(
        local_server._LetterGateway(local_server.letters_adapter).complete(
            [{"role": "user", "content": "synthetic input"}],
            request_id=durable_request_id,
        )
    )

    assert response.request_id == durable_request_id
    assert seen_request_ids == [durable_request_id]


def test_normal_send_list_and_detail_preserve_legacy_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    import local_server

    monkeypatch.setattr(
        local_server.letters_adapter,
        "reply",
        lambda *_args, **_kwargs: "synthetic reply",
    )
    sent = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            {"content": "synthetic input", "material": {"stamp_id": "fixture-stamp"}},
            {},
        )
    )
    letter_id = sent["data"]["letter_id"]
    listed = asyncio.run(local_server.route("GET", "/toy/letter/list", {}, {}))
    detail = asyncio.run(
        local_server.route("GET", "/toy/letter/detail", {}, {"letter_id": letter_id})
    )

    assert sent["code"] == 0
    assert sent["data"]["letterId"] == letter_id
    assert listed["data"]["list"][0]["letter_id"] == letter_id
    assert detail["data"]["content"] == "synthetic input"
    assert detail["data"]["reply_text"] == "synthetic reply"
    assert detail["data"]["reply_content"] == "synthetic reply"
    assert detail["data"]["read_only"] is False


def test_send_accepts_only_the_sixty_second_video_music_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    monkeypatch.setattr(
        local_server.letters_adapter,
        "reply",
        lambda *_args, **_kwargs: "synthetic reply",
    )

    rejected = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            {"content": "synthetic input", "material": {"music_duration_seconds": 118}},
            {},
        )
    )
    rejected_legacy_short = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            {"content": "synthetic input", "material": {"music_duration_seconds": 40}},
            {},
        )
    )
    accepted = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            {"content": "synthetic input", "material": {"music_duration_seconds": 60}},
            {},
        )
    )

    assert rejected == {
        "code": 400,
        "message": "MUSIC_DURATION_INVALID",
        "data": {
            "status": "FAILED",
            "error_code": "MUSIC_DURATION_INVALID",
            "allowed": [60],
        },
    }
    assert rejected_legacy_short == rejected
    assert accepted["code"] == 0


def test_http_send_acknowledges_before_slow_reply_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import local_server

    reply_started = threading.Event()
    allow_reply = threading.Event()

    def slow_reply(*_args, **_kwargs) -> str:
        reply_started.set()
        allow_reply.wait(timeout=2.0)
        return "synthetic delayed reply"

    monkeypatch.setattr(local_server.letters_adapter, "reply", slow_reply)

    async def exercise() -> tuple[dict, dict, dict]:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app, access_log=None)) as client:
            pending_response = asyncio.create_task(
                client.post(
                    "/toy/letter/send",
                    json={"content": "synthetic slow input"},
                )
            )
            try:
                response = await asyncio.wait_for(
                    asyncio.shield(pending_response),
                    timeout=0.2,
                )
                sent = await response.json()
                duplicate_response = await client.post(
                    "/toy/letter/send",
                    json={"content": "synthetic slow input"},
                )
                duplicate = await duplicate_response.json()
            finally:
                allow_reply.set()
            await asyncio.gather(*tuple(local_server.reply_tasks))
            detail_response = await client.get(
                "/toy/letter/detail",
                params={"letter_id": sent["data"]["letter_id"]},
            )
            return sent, duplicate, await detail_response.json()

    sent, duplicate, detail = asyncio.run(exercise())

    assert reply_started.wait(timeout=0.2)
    assert sent["code"] == 0
    assert sent["data"]["status"] == "PENDING"
    assert duplicate["data"]["letter_id"] == sent["data"]["letter_id"]
    assert len(local_server.store.letters) == 1
    assert detail["data"]["letter_status"] == "COMPLETED"
    assert detail["data"]["reply_text"] == "synthetic delayed reply"


def test_http_rejects_a_new_letter_until_the_current_reply_is_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import local_server

    reply_started = threading.Event()
    allow_reply = threading.Event()

    def slow_reply(*_args, **_kwargs) -> str:
        reply_started.set()
        allow_reply.wait(timeout=2.0)
        return "synthetic delayed reply"

    monkeypatch.setattr(local_server.letters_adapter, "reply", slow_reply)
    monkeypatch.setenv("OLIVIA_REPLY_DELAY_ENABLED", "1")
    monkeypatch.setenv("OLIVIA_REPLY_DELAY_MINUTES_MIN", "5")
    monkeypatch.setenv("OLIVIA_REPLY_DELAY_MINUTES_MAX", "5")

    async def exercise() -> tuple[dict, int, dict, int, dict, int, dict]:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app, access_log=None)) as client:
            first_response = await client.post(
                "/toy/letter/send",
                json={
                    "content": "synthetic first letter",
                    "idempotency_key": "single-active:first",
                },
            )
            first = await first_response.json()
            assert reply_started.wait(timeout=0.2)

            duplicate_response = await client.post(
                "/toy/letter/send",
                json={
                    "content": "synthetic first letter",
                    "idempotency_key": "single-active:first",
                },
            )
            duplicate = await duplicate_response.json()

            blocked_while_processing_response = await client.post(
                "/toy/letter/send",
                json={
                    "content": "synthetic second letter",
                    "idempotency_key": "single-active:second",
                },
            )
            blocked_while_processing = await blocked_while_processing_response.json()

            allow_reply.set()
            await asyncio.gather(*tuple(local_server.reply_tasks))

            blocked_before_delivery_response = await client.post(
                "/toy/letter/send",
                json={
                    "content": "synthetic third letter",
                    "idempotency_key": "single-active:third",
                },
            )
            blocked_before_delivery = await blocked_before_delivery_response.json()
            local_server.store.letters[0]["reply_not_before"] = 0.0
            delivered_response = await client.post(
                "/toy/letter/send",
                json={"content": "synthetic first letter"},
            )
            delivered = await delivered_response.json()
            await asyncio.gather(*tuple(local_server.reply_tasks))
            return (
                first,
                blocked_while_processing_response.status,
                blocked_while_processing,
                blocked_before_delivery_response.status,
                blocked_before_delivery,
                delivered_response.status,
                delivered,
            ), duplicate

    (
        (
            first,
            processing_status,
            blocked_while_processing,
            delivery_status,
            blocked_before_delivery,
            delivered_status,
            delivered,
        ),
        duplicate,
    ) = asyncio.run(exercise())

    assert first["data"]["status"] == "PENDING"
    assert duplicate["data"]["letter_id"] == first["data"]["letter_id"]
    assert processing_status == 409
    assert blocked_while_processing == {
        "code": 409,
        "message": "LETTER_IN_PROGRESS",
        "data": {
            "status": "FAILED",
            "error_code": "LETTER_IN_PROGRESS",
            "retryable": True,
            "active_letter_id": first["data"]["letter_id"],
        },
    }
    assert delivery_status == 409
    assert blocked_before_delivery == blocked_while_processing
    assert delivered_status == 200
    assert delivered["data"]["letter_id"] != first["data"]["letter_id"]
    assert len(local_server.store.letters) == 2

    import http_contract

    assert http_contract.error_metadata("LETTER_IN_PROGRESS") == {
        "http_status": 409,
        "retryable": True,
    }


def test_persisted_pending_reply_resumes_when_http_runtime_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import local_server

    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    generated_contents: list[str] = []

    def recovered_reply(content: str, *_args, **_kwargs) -> str:
        generated_contents.append(content)
        return "synthetic recovered reply"

    monkeypatch.setattr(local_server.letters_adapter, "reply", recovered_reply)
    pending = {
        "letter_id": "letter-restart-pending",
        "content": "synthetic persisted input",
        "material": {},
        "letter_status": "PENDING",
        "audit_status": 2,
        "is_read": 1,
        "created_at": int(time.time()),
        "reply_text": "",
        "reply_mode": "text_letter",
        "triage": {"status": "pending"},
        "music_duration_seconds": 60,
    }
    local_server.store.letters.append(pending)
    local_server.store.letters.append({
        **pending,
        "letter_id": "letter-restart-uncertain",
        "content": "synthetic uncertain provider input",
        "letter_status": "PROCESSING",
        "reply_mode": "musical_video",
        "media_status": "PENDING",
        "media_error_code": "STALE_MEDIA_ERROR",
        "media_retryable": True,
    })
    local_server.store.request_keys["restart-key"] = pending["letter_id"]
    local_server._persist_store_state()
    local_server.store.letters.clear()
    local_server.store.request_keys.clear()
    local_server._load_store_state()

    async def exercise() -> dict:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        local_server.install_reply_task_lifecycle(app)
        async with TestClient(TestServer(app, access_log=None)) as client:
            await asyncio.gather(*tuple(local_server.reply_tasks))
            response = await client.get(
                "/toy/letter/detail",
                params={"letter_id": pending["letter_id"]},
            )
            interrupted_response = await client.get(
                "/toy/letter/detail",
                params={"letter_id": "letter-restart-uncertain"},
            )
            return await response.json(), await interrupted_response.json()

    detail, interrupted = asyncio.run(exercise())
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))

    assert detail["data"]["letter_status"] == "COMPLETED"
    assert detail["data"]["reply_text"] == "synthetic recovered reply"
    assert interrupted["data"]["letter_status"] == "FAILED"
    assert interrupted["data"]["error_code"] == "LLM_INTERRUPTED"
    assert interrupted["data"]["media_status"] == "NOT_REQUESTED"
    assert interrupted["data"]["media_error_code"] is None
    assert interrupted["data"]["media_retryable"] is False
    assert generated_contents == ["synthetic persisted input"]
    persisted_by_id = {item["letter_id"]: item for item in persisted["letters"]}
    assert persisted_by_id[pending["letter_id"]]["letter_status"] == "COMPLETED"
    assert persisted_by_id["letter-restart-uncertain"]["letter_status"] == "FAILED"
    assert (
        persisted_by_id["letter-restart-uncertain"]["media_status"]
        == "NOT_REQUESTED"
    )
    assert "media_error_code" not in persisted_by_id["letter-restart-uncertain"]
    assert persisted_by_id["letter-restart-uncertain"]["media_retryable"] is False
    assert persisted["request_keys"]["restart-key"] == pending["letter_id"]


@pytest.mark.parametrize(
    "corrupt_state",
    [b'{"letters":[', b'{"letters":"not-a-list"}'],
)
def test_corrupt_state_blocks_public_letter_mutation_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupt_state: bytes,
) -> None:
    import http_contract
    import local_server

    state_path = tmp_path / "state.json"
    state_path.write_bytes(corrupt_state)
    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(local_server, "_store_state_error_code", None, raising=False)
    monkeypatch.setattr(local_server, "_schedule_reply_job", lambda *_args, **_kwargs: None)
    local_server._load_store_state()

    *current_reads, legacy_read = _get_mailbox_responses(local_server)
    status, payload = _post_current_letter(
        local_server, "synthetic state recovery letter"
    )

    assert all(
        read_status == 503
        and read_payload["data"]["error_code"] == "STORE_STATE_UNAVAILABLE"
        for read_status, read_payload in current_reads
    )
    assert legacy_read[0] == 200
    assert legacy_read[1]["data"]["scope"] == "legacy"
    assert status == 503
    assert payload["data"]["error_code"] == "STORE_STATE_UNAVAILABLE"
    assert state_path.read_bytes() == corrupt_state
    assert local_server.store.letters == []
    assert local_server.store.request_keys == {}
    assert http_contract.error_metadata("STORE_STATE_UNAVAILABLE") == {
        "http_status": 503,
        "retryable": False,
    }
    error_table = (ROOT / "docs" / "B02_ERROR_CODES.md").read_text(encoding="utf-8")
    assert "| 503 | `STORE_STATE_UNAVAILABLE` | FAILED | 否 |" in error_table


@pytest.mark.parametrize("rewrite_fails", [False, True])
def test_corrupt_primary_state_recovers_from_durable_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rewrite_fails: bool,
) -> None:
    import local_server

    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(local_server, "_store_state_error_code", None, raising=False)
    letter = {
        "letter_id": "synthetic-backup-letter",
        "letter_status": "COMPLETED",
        "reply_text": "synthetic backup reply",
    }
    local_server.store.letters.append(letter)
    local_server._persist_store_state()
    state_path = tmp_path / "state.json"
    state_path.write_bytes(b'{"letters":[')
    local_server.store.letters.clear()
    if rewrite_fails:
        real_replace = local_server._os.replace

        def reject_primary_rewrite(source, destination) -> None:
            if Path(destination) == state_path:
                raise OSError("synthetic primary rewrite failure")
            real_replace(source, destination)

        monkeypatch.setattr(local_server._os, "replace", reject_primary_rewrite)
    local_server._load_store_state()

    *current_reads, legacy_read = _get_mailbox_responses(local_server, letter["letter_id"])
    status, payload = current_reads[2]

    if rewrite_fails:
        assert all(item[0] == 503 for item in current_reads)
        assert legacy_read[0] == 200
        assert state_path.read_bytes() == b'{"letters":['
    else:
        assert status == 200
        assert payload["data"]["reply_text"] == "synthetic backup reply"
        assert json.loads(state_path.read_text(encoding="utf-8"))["letters"][0][
            "letter_id"
        ] == letter["letter_id"]


@pytest.mark.parametrize("existing_snapshot", [True, False])
def test_public_letter_mutation_rolls_back_when_state_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_snapshot: bool,
) -> None:
    import os

    import local_server

    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(local_server, "_store_state_error_code", None, raising=False)
    monkeypatch.setattr(local_server, "_schedule_reply_job", lambda *_args, **_kwargs: None)
    state_path = tmp_path / "state.json"
    if existing_snapshot:
        local_server._persist_store_state()
    previous_state = state_path.read_bytes() if existing_snapshot else None
    real_replace = os.replace

    def reject_primary_replace(source, destination) -> None:
        if Path(destination) == state_path:
            raise OSError("synthetic replace failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", reject_primary_replace)

    status, payload = _post_current_letter(
        local_server, "synthetic failed persistence letter"
    )

    assert status == 503
    assert payload["data"]["error_code"] == "STORE_STATE_UNAVAILABLE"
    if previous_state is None:
        assert not state_path.exists()
    else:
        assert state_path.read_bytes() == previous_state
    assert local_server.store.letters == []
    assert not tuple(tmp_path.glob("*.tmp"))


def test_state_persist_fsyncs_unique_snapshots_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    import local_server

    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(local_server, "_store_state_error_code", None, raising=False)
    events: list[tuple[str, str]] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def record_fsync(descriptor: int) -> None:
        events.append(("fsync", ""))
        real_fsync(descriptor)

    def record_replace(source, destination) -> None:
        events.append(("replace", Path(source).name))
        real_replace(source, destination)

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "replace", record_replace)

    local_server._persist_store_state()

    write_events = ["fsync", "replace"]
    if os.name != "nt":
        write_events.append("fsync")
    assert [event[0] for event in events] == write_events * 2
    temporary_names = [value for event, value in events if event == "replace"]
    assert len(set(temporary_names)) == 2
    assert ".state.json.tmp" not in temporary_names
    assert ".state.json.bak.tmp" not in temporary_names


def test_directory_fsync_failure_keeps_committed_letter_scheduled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    scheduled: list[str] = []
    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(local_server, "_store_state_error_code", None, raising=False)
    monkeypatch.setattr(
        local_server,
        "_schedule_reply_job",
        lambda letter_id, *_args, **_kwargs: scheduled.append(letter_id),
    )
    monkeypatch.setattr(
        local_server,
        "_fsync_store_directory",
        lambda *_args: (_ for _ in ()).throw(OSError("synthetic directory fsync failure")),
    )

    status, payload = _post_current_letter(
        local_server, "synthetic durability uncertain letter"
    )

    assert status == 200
    assert payload["data"]["status"] == "PENDING"
    assert scheduled == [payload["data"]["letter_id"]]
    assert local_server.store.letters == json.loads(
        (tmp_path / "state.json").read_text(encoding="utf-8")
    )["letters"]


def test_public_letter_mutation_normalizes_state_root_creation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    blocked_root = tmp_path / "blocked-state-root"
    blocked_root.write_text("synthetic blocker", encoding="utf-8")
    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(blocked_root))
    monkeypatch.setattr(local_server, "_store_state_error_code", None, raising=False)
    monkeypatch.setattr(local_server, "_schedule_reply_job", lambda *_args, **_kwargs: None)

    status, payload = _post_current_letter(
        local_server, "synthetic blocked state root letter"
    )

    assert status == 503
    assert payload["data"]["error_code"] == "STORE_STATE_UNAVAILABLE"
    assert str(blocked_root) not in json.dumps(payload)
    assert local_server.store.letters == []


@pytest.mark.parametrize(
    ("failed_target", "expected_status"),
    [("state.json", 503), ("state.json.bak", 200)],
)
def test_state_temp_creation_failure_preserves_primary_commit_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_target: str,
    expected_status: int,
) -> None:
    import local_server

    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(local_server, "_store_state_error_code", None, raising=False)
    monkeypatch.setattr(local_server, "_schedule_reply_job", lambda *_args, **_kwargs: None)
    real_mkstemp = local_server._tempfile.mkstemp

    def selective_mkstemp(*args, **kwargs):
        if kwargs.get("prefix", "").startswith(f".{failed_target}."):
            raise OSError("synthetic temp creation failure")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(local_server._tempfile, "mkstemp", selective_mkstemp)

    status, payload = _post_current_letter(
        local_server, "synthetic temp failure letter"
    )

    assert status == expected_status
    if failed_target == "state.json":
        assert payload["data"]["error_code"] == "STORE_STATE_UNAVAILABLE"
        assert local_server.store.letters == []
        assert not (tmp_path / "state.json").exists()
    else:
        assert payload["code"] == 0
        assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))[
            "letters"
        ][0]["content"] == "synthetic temp failure letter"


def test_public_letter_mutation_normalizes_unencodable_state_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(local_server, "_store_state_error_code", None, raising=False)
    monkeypatch.setattr(local_server, "_schedule_reply_job", lambda *_args, **_kwargs: None)

    status, payload = _post_current_letter(local_server, "synthetic\ud800state text")

    assert status == 503
    assert payload["data"]["error_code"] == "STORE_STATE_UNAVAILABLE"
    assert local_server.store.letters == []


def test_failed_idempotent_request_can_retry_with_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    allow_success = False

    def reply(*_args, **_kwargs):
        if not allow_success:
            raise local_server.LLMError("LLM_TIMEOUT")
        return "synthetic recovered reply"

    monkeypatch.setattr(local_server.letters_adapter, "reply", reply)
    body = {
        "content": "synthetic idempotent retry",
        "material": {"stamp_id": "stamp-a"},
        "idempotency_key": "retry-key",
    }

    first = asyncio.run(local_server.route("POST", "/toy/letter/send", body, {}))
    assert local_server.store.letters[0]["letter_status"] == "FAILED"
    allow_success = True
    second = asyncio.run(local_server.route("POST", "/toy/letter/send", body, {}))

    assert first["code"] == 503
    assert second["code"] == 0
    assert second["data"]["status"] == "COMPLETED"
    assert second["data"]["letter_id"] != first["data"]["letter_id"]
    assert local_server.store.request_keys["retry-key"] == second["data"]["letter_id"]


def test_unexpected_background_failure_cannot_leave_processing_letter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import local_server

    async def crash(*_args, **_kwargs):
        raise KeyError("synthetic private provider failure")

    monkeypatch.setattr(local_server.reply_pipeline, "run", crash)

    async def exercise() -> tuple[dict, dict]:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app, access_log=None)) as client:
            sent_response = await client.post(
                "/toy/letter/send",
                json={"content": "synthetic unexpected failure"},
            )
            sent = await sent_response.json()
            await asyncio.gather(*tuple(local_server.reply_tasks))
            detail_response = await client.get(
                "/toy/letter/detail",
                params={"letter_id": sent["data"]["letter_id"]},
            )
            return sent, await detail_response.json()

    sent, detail = asyncio.run(exercise())

    assert sent["data"]["status"] == "PENDING"
    assert detail["data"]["letter_status"] == "FAILED"
    assert detail["data"]["error_code"] == "LLM_UNAVAILABLE"


def test_failed_letter_deadline_does_not_hide_terminal_send_list_or_detail() -> None:
    import local_server

    letter = {
        "letter_id": "failed-before-delivery",
        "content": "synthetic failed input",
        "material": {},
        "letter_status": "FAILED",
        "audit_status": 2,
        "is_read": 1,
        "created_at": 1_700_000_000,
        "reply_text": "",
        "reply_mode": "text_letter",
        "reply_not_before": 4_000_000_000.0,
        "error_code": "REPLY_QUALITY_BLOCKED",
        "media_status": "NOT_REQUESTED",
    }
    local_server.store.letters.append(letter)

    sent = local_server._send_result_for_letter(letter)
    listed = asyncio.run(local_server.route("GET", "/toy/letter/list", {}, {}))
    detail = asyncio.run(
        local_server.route(
            "GET",
            "/toy/letter/detail",
            {},
            {"letter_id": letter["letter_id"]},
        )
    )

    assert sent["code"] == 503
    assert sent["data"]["status"] == "FAILED"
    assert listed["data"]["list"][0]["letter_status"] == "FAILED"
    assert detail["data"]["letter_status"] == "FAILED"
    assert detail["data"]["error_code"] == "REPLY_QUALITY_BLOCKED"


@pytest.mark.parametrize("status", ["CANCELED", "CANCELLED"])
def test_canceled_letter_deadline_preserves_terminal_send_status(
    status: str,
) -> None:
    import local_server

    letter = {
        "letter_id": f"{status.casefold()}-before-delivery",
        "letter_status": status,
        "reply_not_before": 4_000_000_000.0,
    }

    sent = local_server._send_result_for_letter(letter)

    assert sent["code"] == 0
    assert sent["data"]["status"] == status
    assert sent["data"]["letter_id"] == letter["letter_id"]


def test_persona_not_ready_failure_persists_and_round_trips_through_http_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import local_server
    from reply_orchestrator import ReplyState
    from reply_pipeline import PipelineResult

    private_body = "PRIVATE_PERSONA_BODY"
    private_path = str(tmp_path / "private-persona.json")
    parse_detail = "JSONDecodeError:line7"

    class PersonaNotReadyPipeline:
        async def run(self, *_args, **_kwargs) -> PipelineResult:
            return PipelineResult(
                "persona-not-ready", ReplyState.FAILED,
                text=private_body, error_code="PERSONA_NOT_READY", retryable=False,
                quality_status=f"{private_path}:{parse_detail}",
            )

    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(local_server, "reply_pipeline", PersonaNotReadyPipeline())

    async def exercise() -> tuple[int, dict, dict]:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app, access_log=None)) as client:
            sent = await (await client.post(
                "/toy/letter/send", json={"content": "synthetic route input"},
            )).json()
            await asyncio.gather(*tuple(local_server.reply_tasks))
            persisted = json.loads((tmp_path / "state.json").read_text("utf-8"))
            local_server.store.letters.clear()
            local_server._load_store_state()
            response = await client.get(
                "/toy/letter/detail",
                params={"letter_id": sent["data"]["letter_id"]},
            )
            return response.status, await response.json(), persisted

    status, detail, persisted = asyncio.run(exercise())
    public = json.dumps(detail, ensure_ascii=False)

    assert status == 200
    assert detail["code"] == 0
    assert detail["data"]["letter_status"] == "FAILED"
    assert detail["data"]["error_code"] == "PERSONA_NOT_READY"
    assert detail["data"]["retryable"] is False
    assert detail["data"]["reply_text"] == ""
    assert all(value not in public for value in (private_body, private_path, parse_detail))
    persisted_json = json.dumps(persisted)
    assert private_body not in persisted_json
    quality_status = persisted["letters"][0]["quality_status"]
    assert private_path in quality_status and parse_detail in quality_status


def test_retry_dedup_does_not_block_distinct_expired_or_failed_letters() -> None:
    import local_server

    original = {
        "letter_id": "letter-original",
        "content": "synthetic input",
        "material": {"stamp_id": "stamp-a"},
        "letter_status": "COMPLETED",
        "created_at": 100,
        "reply_not_before": 200,
    }
    local_server.store.letters.append(original)

    assert (
        local_server._recent_active_duplicate(
            "synthetic input",
            {"stamp_id": "stamp-a"},
            now=159,
        )
        is original
    )
    assert (
        local_server._recent_active_duplicate(
            "synthetic input",
            {"stamp_id": "stamp-b"},
            now=159,
        )
        is None
    )
    assert (
        local_server._recent_active_duplicate(
            "synthetic input",
            {"stamp_id": "stamp-a"},
            now=161,
        )
        is None
    )
    original["reply_not_before"] = 0
    assert (
        local_server._recent_active_duplicate(
            "synthetic input",
            {"stamp_id": "stamp-a"},
            now=159,
        )
        is None
    )
    original["letter_status"] = "FAILED"
    assert (
        local_server._recent_active_duplicate(
            "synthetic input",
            {"stamp_id": "stamp-a"},
            now=159,
        )
        is None
    )


def test_missing_fields_and_invalid_json_never_become_success() -> None:
    import local_server

    missing_detail = asyncio.run(
        local_server.route("GET", "/toy/letter/detail", {}, {})
    )
    missing_send = asyncio.run(local_server.route("POST", "/toy/letter/send", {}, {}))
    bad_material = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            {"content": "synthetic", "material": []},
            {},
        )
    )

    assert missing_detail["code"] == 400
    assert missing_detail["data"]["error_code"] == "MISSING_FIELD"
    assert missing_send["code"] == 400
    assert missing_send["data"]["error_code"] == "MISSING_FIELD"
    assert bad_material["code"] == 400
    assert bad_material["data"]["error_code"] == "INVALID_FIELD_TYPE"


def test_handler_rejects_malformed_json_and_wrong_methods() -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import local_server

    async def exercise():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app, access_log=None)) as client:
            malformed = await client.post("/toy/letter/send", data="{")
            wrong_method = await client.post("/toy/letter/list", json={})
            return (
                malformed.status,
                (await malformed.json())["data"]["error_code"],
                wrong_method.status,
                (await wrong_method.json())["data"]["error_code"],
            )

    assert asyncio.run(exercise()) == (400, "INVALID_JSON", 405, "METHOD_NOT_ALLOWED")


def test_llm_failure_can_be_retried_through_the_resend_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    outcomes: list[str | Exception] = [
        local_server.LLMError("LLM_TIMEOUT"),
        "synthetic successful resend",
    ]

    def reply(_content, _context="", **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(local_server.letters_adapter, "reply", reply)
    failed = asyncio.run(
        local_server.route(
            "POST", "/toy/letter/send", {"content": "synthetic retry input"}, {}
        )
    )
    first_retry = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/resend",
            {"letter_id": failed["data"]["letter_id"]},
            {},
        )
    )
    second_retry = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/resend",
            {"letter_id": failed["data"]["letter_id"]},
            {},
        )
    )

    assert failed["code"] == 503
    assert failed["data"]["error_code"] == "LLM_TIMEOUT"
    assert failed["data"]["retryable"] is True
    assert first_retry["code"] == 0
    assert first_retry["data"]["status"] == "COMPLETED"
    assert second_retry["code"] == 410
    assert second_retry["data"]["error_code"] == "LETTER_SUPERSEDED"
    assert len(local_server.store.letters) == 2


def test_user_can_manually_resend_a_provider_rejected_letter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    outcomes: list[str | Exception] = [
        local_server.LLMError("LLM_PROVIDER_REJECTED"),
        "synthetic successful manual resend",
    ]

    def reply(_content, _context="", **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(local_server.letters_adapter, "reply", reply)
    failed = asyncio.run(
        local_server.route(
            "POST", "/toy/letter/send", {"content": "synthetic manual retry"}, {}
        )
    )
    retried = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/resend",
            {"letter_id": failed["data"]["letter_id"]},
            {},
        )
    )

    assert failed["code"] == 503
    assert failed["data"]["error_code"] == "LLM_PROVIDER_REJECTED"
    assert failed["data"]["retryable"] is False
    assert retried["code"] == 0
    assert retried["data"]["status"] == "COMPLETED"


def test_http_manual_resend_acknowledges_before_background_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import local_server

    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    failed_id = "synthetic-provider-rejected"
    local_server.store.letters[:] = [
        {
            "letter_id": failed_id,
            "content": "synthetic manual resend",
            "material": {},
            "letter_status": "FAILED",
            "error_code": "LLM_PROVIDER_REJECTED",
            "audit_status": 2,
            "is_read": 1,
            "created_at": int(time.time()),
            "reply_text": "",
        }
    ]
    scheduled: list[str] = []
    monkeypatch.setattr(
        local_server,
        "_schedule_reply_job",
        lambda letter_id, *_args, **_kwargs: scheduled.append(letter_id),
    )

    async def exercise() -> tuple[int, dict]:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app, access_log=None)) as client:
            response = await client.post(
                "/toy/letter/resend", json={"letter_id": failed_id}
            )
            return response.status, await response.json()

    status, payload = asyncio.run(exercise())

    assert status == 200
    assert payload["data"]["status"] == "PENDING"
    assert scheduled == [payload["data"]["letter_id"]]


def test_successful_retry_replaces_recent_failed_copy_in_current_mailbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    outcomes: list[str | Exception] = [
        local_server.LLMError("LLM_TIMEOUT"),
        "synthetic successful retry",
    ]

    def reply(_content, _context="", **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(local_server.letters_adapter, "reply", reply)
    failed = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            {"content": "synthetic retried letter"},
            {},
        )
    )
    failed_state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    retried = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            {"content": "synthetic retried letter"},
            {},
        )
    )
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    local_server.store.letters.clear()
    local_server._load_store_state()
    listed = asyncio.run(
        local_server.route("GET", "/toy/letter/list", {}, {"scope": "current"})
    )
    old_detail = asyncio.run(
        local_server.route(
            "GET",
            "/toy/letter/detail",
            {},
            {"scope": "current", "letter_id": failed["data"]["letter_id"]},
        )
    )

    async def fetch_http_tombstone() -> tuple[int, dict]:
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app, access_log=None)) as client:
            response = await client.get(
                "/toy/letter/detail",
                params={
                    "scope": "current",
                    "letter_id": failed["data"]["letter_id"],
                },
            )
            return response.status, await response.json()

    tombstone_status, tombstone_payload = asyncio.run(fetch_http_tombstone())

    assert failed["code"] == 503
    assert failed_state["letters"][0]["letter_status"] == "FAILED"
    assert retried["code"] == 0
    assert listed["data"]["total"] == 1
    assert [item["letter_id"] for item in listed["data"]["list"]] == [
        retried["data"]["letter_id"]
    ]
    failed_record = next(
        item
        for item in persisted["letters"]
        if item["letter_id"] == failed["data"]["letter_id"]
    )
    assert failed_record["letter_status"] == "FAILED"
    assert failed_record["superseded_by"] == retried["data"]["letter_id"]
    assert old_detail["code"] == 410
    assert old_detail["data"] == {
        "status": "SUPERSEDED",
        "error_code": "LETTER_SUPERSEDED",
        "replacement_letter_id": retried["data"]["letter_id"],
    }
    assert tombstone_status == 410
    assert tombstone_payload == old_detail


def test_legacy_scope_is_read_only_and_isolated_from_new_chat() -> None:
    import local_server

    fixture = json.loads(
        (ROOT / "contracts" / "legacy_letter_import.example.json").read_text(encoding="utf-8")
    )
    local_server.store.legacy_letters.extend(fixture["letters"])
    local_server.store.letters.append(
        {
            "letter_id": "current-fixture-letter-001",
            "content": "current synthetic body",
            "reply_text": "current synthetic reply",
            "is_read": 1,
            "letter_status": "COMPLETED",
            "audit_status": 2,
        }
    )

    current = asyncio.run(
        local_server.route("GET", "/toy/letter/list", {}, {"scope": "current"})
    )
    legacy = asyncio.run(
        local_server.route("GET", "/toy/letter/list", {}, {"scope": "legacy"})
    )
    legacy_detail = asyncio.run(
        local_server.route(
            "GET",
            "/toy/letter/detail",
            {},
            {"scope": "legacy", "letter_id": "legacy-fixture-letter-001"},
        )
    )
    denied_send = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            {"content": "must not write legacy"},
            {"scope": "legacy"},
        )
    )

    assert [item["letter_id"] for item in current["data"]["list"]] == [
        "current-fixture-letter-001"
    ]
    assert [item["letter_id"] for item in legacy["data"]["list"]] == [
        "legacy-fixture-letter-001"
    ]
    assert legacy["data"]["read_only"] is True
    assert legacy_detail["data"]["is_read"] == 0
    assert local_server.store.legacy_letters[0]["is_read"] == 0
    assert denied_send["code"] == 403
    assert denied_send["data"]["error_code"] == "READ_ONLY_SCOPE"


def test_unimplemented_routes_and_native_capabilities_are_not_fake_successes() -> None:
    import local_server

    paths = (
        "/toy/letter/share",
        "/toy/addPerformance",
        "/toy/genObjectUploadUrl",
        "/toy/midi/importShareCode",
        "/toy/not-known",
    )
    results = [
        asyncio.run(local_server.route("POST", path, {}, {}))
        for path in paths
    ]

    assert all(result["code"] == 501 for result in results)
    assert all(result["data"]["status"] == "NOT_IMPLEMENTED" for result in results)
    invalid_import = asyncio.run(
        local_server.route("POST", "/toy/letter/legacy/import", {}, {})
    )
    assert invalid_import["code"] == 400
    assert invalid_import["data"]["error_code"] == "INVALID_BODY"
    health = asyncio.run(local_server.route("GET", "/health", {}, {}))
    assert health["data"]["capabilities"]["native.websocket"]["status"] == "unavailable"
    assert health["data"]["capabilities"]["native.asr"]["status"] == "unavailable"
    assert health["data"]["capabilities"]["native.tts"]["status"] == "unavailable"
    assert health["data"]["capabilities"]["native.live"]["status"] == "unavailable"


def test_legacy_import_validation_and_storage_errors_do_not_echo_request_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server
    from memory_port import NullMemoryPort

    blocked_root = tmp_path / "not-a-directory"
    blocked_root.write_text("synthetic blocker", encoding="utf-8")
    monkeypatch.setenv("OLIVIA_MEMORY_ROOT", str(blocked_root))
    monkeypatch.setattr(local_server, "memory_adapter", NullMemoryPort())

    invalid = asyncio.run(
        local_server.route("POST", "/toy/letter/legacy/import", {"mode": "write"}, {})
    )
    private_body = "synthetic-import-body-not-for-response"
    credential_marker = "synthetic-credential-not-for-response"
    unavailable = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/legacy/import",
            {
                "mode": "read_only",
                "letters": [
                    {
                        "source_record_id": "synthetic-storage-error",
                        "content": private_body,
                        "metadata": {"credential_fixture": credential_marker},
                    }
                ],
            },
            {},
        )
    )

    assert invalid["code"] == 400
    assert invalid["data"]["error_code"] == "INVALID_BODY"
    assert unavailable["code"] == 503
    assert unavailable["data"]["error_code"] == "MEMORY_UNAVAILABLE"
    assert unavailable["data"]["status"] == "UNAVAILABLE"
    assert unavailable["data"]["retryable"] is True
    serialized = json.dumps(unavailable)
    assert private_body not in serialized
    assert credential_marker not in serialized


def test_mem0_mode_can_incrementally_import_archive_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server
    from local_memory import LocalMemoryAdapter, MemoryConfig, create_memory_adapter
    from memory_port import LegacyLetter

    memory_root = tmp_path / "memory"
    with LocalMemoryAdapter(
        memory_root / "memory.sqlite3",
        conversation_enabled=False,
    ) as initial_archive:
        initial_archive.import_legacy_records(
            [LegacyLetter("first archived letter", "archive-1", "synthetic")]
        )
    restarted_archive = create_memory_adapter(
        MemoryConfig(
            enabled=False,
            provider="sqlite",
            data_root=memory_root,
        )
    )
    monkeypatch.setattr(local_server, "memory_adapter", restarted_archive)
    monkeypatch.setenv("OLIVIA_MEMORY_ENABLED", "true")
    monkeypatch.setenv("OLIVIA_MEMORY_PROVIDER", "mem0")
    monkeypatch.setenv("OLIVIA_MEMORY_ROOT", str(memory_root))

    result = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/legacy/import",
            {
                "mode": "read_only",
                "letters": [
                    {
                        "source_record_id": "archive-2",
                        "source": "synthetic",
                        "content": "second archived letter",
                    }
                ],
            },
            {},
        )
    )

    assert result["code"] == 0
    assert result["data"]["inserted"] == 1
    assert sorted(
        record["source_record_id"] for record in local_server.memory_adapter.list_legacy()
    ) == ["archive-1", "archive-2"]


def test_contract_and_fixture_artifacts_are_versioned_and_sanitized() -> None:
    import http_contract
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (ROOT / "contracts" / "http_contract.schema.json").read_text(encoding="utf-8")
    )
    legacy_schema = json.loads(
        (ROOT / "contracts" / "legacy_letter_import.schema.json").read_text(encoding="utf-8")
    )
    document = http_contract.contract_document()
    example = json.loads(
        (ROOT / "contracts" / "http_contract.example.json").read_text(encoding="utf-8")
    )
    legacy_fixture = json.loads(
        (ROOT / "contracts" / "legacy_letter_import.example.json").read_text(encoding="utf-8")
    )

    assert not list(Draft202012Validator(schema).iter_errors(document))
    assert not list(Draft202012Validator(schema).iter_errors(example))
    missing_media_contract = dict(document)
    missing_media_contract.pop("letter_detail_media")
    assert list(Draft202012Validator(schema).iter_errors(missing_media_contract))
    missing_generation_contract = dict(document)
    missing_generation_contract.pop("letter_detail_generation")
    assert list(Draft202012Validator(schema).iter_errors(missing_generation_contract))

    assert schema["properties"]["schema_version"]["const"] == 2
    assert schema["properties"]["contract_version"]["const"] == "b02.v2"
    assert legacy_schema["properties"]["mode"]["const"] == "read_only"
    assert document["schema_version"] == example["schema_version"] == 2
    assert document["contract_version"] == example["contract_version"] == "b02.v2"
    assert "/toy/letter/list" in document["routes"]
    assert http_contract.error_metadata("MEMORY_UNAVAILABLE") == {
        "http_status": 503,
        "retryable": True,
    }
    assert http_contract.error_metadata("TTS_CONTENT_GATE_UNAVAILABLE") == {
        "http_status": 200,
        "retryable": True,
    }
    assert http_contract.error_metadata("TTS_CONTENT_GATE_REJECTED") == {
        "http_status": 200,
        "retryable": False,
    }
    assert document["letter_detail_media"] == {
        "fields": [
            "media_status",
            "media_error_code",
            "media_retryable",
            "audio_provider",
            "reply_structure",
            "song_emotion",
            "transition_seconds",
        ],
        "statuses": [
            "NOT_REQUESTED",
            "PENDING",
            "PROCESSING",
            "COMPLETED",
            "FAILED",
            "UNAVAILABLE",
        ],
        "error_codes": {
            "BREEZE_TTS_10GB_VRAM_REQUIRED": {
                "status": "UNAVAILABLE",
                "retryable": True,
            },
            "BREEZE_TTS_GPU_CAPABILITY_UNVERIFIED": {
                "status": "UNAVAILABLE",
                "retryable": True,
            },
            "BREEZE_TTS_NVIDIA_GPU_REQUIRED": {
                "status": "UNAVAILABLE",
                "retryable": True,
            },
            "MEDIA_PROVIDER_UNAVAILABLE": {"status": "UNAVAILABLE", "retryable": True},
            "TTS_CONTENT_GATE_UNAVAILABLE": {
                "status": "UNAVAILABLE",
                "retryable": True,
            },
            "TTS_CONTENT_GATE_REJECTED": {
                "status": "FAILED",
                "retryable": False,
            },
        },
    }
    generation_contract = {
        "fields": ["letter_status", "error_code", "retryable"],
        "error_codes": {
            "MEMORY_UNAVAILABLE": {"status": "FAILED", "retryable": True},
            "LLM_UNAVAILABLE": {"status": "FAILED", "retryable": True},
            "LLM_TIMEOUT": {"status": "FAILED", "retryable": True},
            "LLM_INTERRUPTED": {"status": "FAILED", "retryable": True},
            "LLM_PROVIDER_REJECTED": {"status": "FAILED", "retryable": False},
            "LLM_PROTOCOL_ERROR": {"status": "FAILED", "retryable": False},
            "LLM_REPLY_LENGTH_INVALID": {"status": "FAILED", "retryable": False},
            "REPLY_QUALITY_BLOCKED": {"status": "FAILED", "retryable": False},
            "PERSONA_NOT_READY": {"status": "FAILED", "retryable": False},
        },
    }
    assert document["letter_detail_generation"] == generation_contract
    assert example["letter_detail_generation"] == generation_contract
    assert "LEGACY_IMPORT_NOT_IMPLEMENTED" not in http_contract.ERROR_CODES
    expected_import_mode = "read-only-atomic-import"
    assert schema["properties"]["privacy"]["properties"]["legacy_import_mode"]["const"] == expected_import_mode
    assert document["privacy"]["legacy_import_mode"] == expected_import_mode
    assert example["privacy"]["legacy_import_mode"] == expected_import_mode
    for artifact in (document, example):
        import_route = artifact["routes"]["/toy/letter/legacy/import"]
        import_capability = artifact["capabilities"]["letters.legacy_import"]
        assert import_route["state"] == "available"
        assert import_route["error_code"] is None
        assert import_capability["status"] == "available"
        assert import_capability["provider"] == "sqlite"
        assert import_capability["mode"] == expected_import_mode
    assert legacy_fixture["mode"] == "read_only"
    assert "original" not in json.dumps(legacy_fixture).lower()
    assert "private" not in json.dumps(legacy_fixture).lower()


def test_quality_blocked_generation_error_is_documented_as_sanitized_terminal_detail() -> None:
    table = (ROOT / "docs" / "B02_ERROR_CODES.md").read_text(encoding="utf-8")
    row = next(line for line in table.splitlines() if "`REPLY_QUALITY_BLOCKED`" in line)

    assert "| 200 detail |" in row
    assert "| FAILED | 否 |" in row
    assert "不回显" in row


def test_b02_current_release_paths_are_exactly_owned(monkeypatch) -> None:
    import tools.scope_compat as scope_compat
    import tools.verify_b02_scope as b02_scope

    expected = frozenset(
        {
            "INSTALL.cmd",
            "START.cmd",
            "UNINSTALL.cmd",
            "contracts/http_contract.example.json",
            "contracts/http_contract.schema.json",
            "docs/B02_ERROR_CODES.md",
            "docs/B02_HTTP_CONTRACT.md",
            "docs/WINDOWS_FULL_PATCH.md",
            "http_contract.py",
            "installer/Install.ps1",
            "installer/__init__.py",
            "installer/__main__.py",
            "installer/configure.py",
            "installer/full-patch-manifest.json",
            "installer/full_patch.py",
            "installer/runtime-requirements.txt",
            "installer/start_local.py",
            "installer/uninstall.py",
            "latentsync_reply.py",
            "letter_triage.py",
            "local_server.py",
            "runtime/media/music_duration.py",
            "tools/music_renderer.py",
            "music_reply.py",
            "patch_feapp.py",
            "pyproject.toml",
            "reply_delivery.py",
            "reply_media.py",
            "song_content.py",
            "tests/http/test_contract.py",
            "tests/http/test_letter_triage_portable.py",
            "tests/http/test_reply_delay_and_media.py",
            "tests/installer/test_windows_full_patch.py",
            "tests/media/test_portable_media_boundaries.py",
            "tests/test_baseline_hardening.py",
            "requirements-ci.txt",
            "tools/Install-ThirdParty.ps1",
            "tools/minimax_music3_worker.py",
            "tools/minimax_profile.py",
            "tools/verify_b02_scope.py",
            "tts/delivery.py",
        }
    )

    def fake_git(*args: str) -> str:
        if args[:2] == ("diff", "--name-only"):
            return "\n".join(sorted(expected | {"minimax_profile.py"}))
        if args[:2] == ("status", "--short"):
            return ""
        return ""

    monkeypatch.setattr(b02_scope, "_git", fake_git)
    monkeypatch.setattr(
        scope_compat,
        "effective_scope_base",
        lambda _fallback, _head="HEAD": "base",
    )

    assert b02_scope.current_b02_paths() == expected
    assert "minimax_profile.py" not in b02_scope.ALLOWED_MUTATIONS


def test_b02_current_candidates_include_exact_ci_dependency_owner(monkeypatch) -> None:
    import tools.scope_compat as scope_compat
    import tools.verify_b02_scope as b02_scope

    def fake_git(*args: str) -> list[str]:
        if args[:2] == ("diff", "--name-only"):
            return "requirements-ci.txt\nbaseline_hardening_scan.py\nrandom.py\n"
        if args[:2] == ("status", "--short"):
            return ""
        return ""

    monkeypatch.setattr(b02_scope, "_git", fake_git)
    monkeypatch.setattr(
        scope_compat,
        "effective_scope_base",
        lambda _fallback, _head="HEAD": "base",
    )

    candidates = b02_scope.current_b02_paths()

    assert candidates == frozenset({"requirements-ci.txt"})


def test_b02_current_candidates_include_exact_release_installer_owner(monkeypatch) -> None:
    import tools.scope_compat as scope_compat
    import tools.verify_b02_scope as b02_scope

    def fake_git(*args: str) -> str:
        if args[:2] == ("diff", "--name-only"):
            return "tools/Install-ThirdParty.ps1\ntools/Install-ThirdParty.ps1.bak\nrandom.py\n"
        if args[:2] == ("status", "--short"):
            return ""
        return ""

    monkeypatch.setattr(b02_scope, "_git", fake_git)
    monkeypatch.setattr(
        scope_compat,
        "effective_scope_base",
        lambda _fallback, _head="HEAD": "base",
    )

    assert b02_scope.current_b02_paths() == frozenset({"tools/Install-ThirdParty.ps1"})


@pytest.mark.experimental
def test_b02_scope_integrity_keeps_unrelated_tracked_files_equal_to_head() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "verify_b02_scope", ROOT / "tools" / "verify_b02_scope.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from tools.verify_b05_scope import current_b05_paths

    report = module.check_scope(ROOT, excluded=current_b05_paths())

    assert report["status"] == "PASS"
    assert report["baseline_exact"] is True
    assert report["mismatches"] == []


def test_request_and_reply_values_never_enter_runtime_logs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import local_server

    monkeypatch.setattr(
        local_server.letters_adapter,
        "reply",
        lambda *_args, **_kwargs: "synthetic reply secret",
    )
    capsys.readouterr()

    async def exercise():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app, access_log=None)) as client:
            response = await client.post(
                "/toy/letter/send?token=synthetic-query-token&note=synthetic-query-note",
                json={
                    "content": "synthetic private body",
                    "material": {"token": "synthetic-body-token"},
                },
            )
            return response.status, await response.json()

    status, payload = asyncio.run(exercise())
    logs = capsys.readouterr().out

    assert status == 200
    assert payload["code"] == 0
    for secret in (
        "synthetic-query-token",
        "synthetic-query-note",
        "synthetic private body",
        "synthetic-body-token",
        "synthetic reply secret",
    ):
        assert secret not in logs
