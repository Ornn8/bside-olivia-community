"""B02 route, error, retry, empty-data, capability, and privacy coverage."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
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


def test_core_health_is_versioned_and_reports_unavailable_optional_capabilities() -> None:
    import local_server

    result = asyncio.run(local_server.route("GET", "/health", {}, {"profile": "core"}))
    data = result["data"]

    assert result["code"] == 0
    assert data["status"] == "HEALTHY"
    assert data["contract_version"] == "b02.v1"
    assert data["schema_version"] == 1
    assert data["privacy"]["logs_include_request_body"] is False
    assert data["privacy"]["logs_include_query_values"] is False
    for capability in ("native.websocket", "native.asr", "native.tts", "native.live"):
        assert data["capabilities"][capability]["status"] == "unavailable"


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


def test_send_accepts_only_the_short_music_duration_options(
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
            "allowed": [40, 60],
        },
    }
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
        "created_at": 100,
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
    assert generated_contents == ["synthetic persisted input"]
    persisted_by_id = {item["letter_id"]: item for item in persisted["letters"]}
    assert persisted_by_id[pending["letter_id"]]["letter_status"] == "COMPLETED"
    assert persisted_by_id["letter-restart-uncertain"]["letter_status"] == "FAILED"
    assert persisted["request_keys"]["restart-key"] == pending["letter_id"]


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


def test_retry_dedup_does_not_block_distinct_expired_or_failed_letters() -> None:
    import local_server

    original = {
        "letter_id": "letter-original",
        "content": "synthetic input",
        "material": {"stamp_id": "stamp-a"},
        "letter_status": "COMPLETED",
        "created_at": 100,
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


def test_llm_failure_is_retryable_but_resend_is_explicitly_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server

    def fail(_content, _context="", **_kwargs):
        raise local_server.LLMError("LLM_TIMEOUT")

    monkeypatch.setattr(local_server.letters_adapter, "reply", fail)
    failed = asyncio.run(
        local_server.route(
            "POST", "/toy/letter/send", {"content": "synthetic retry input"}, {}
        )
    )
    first_retry = asyncio.run(
        local_server.route("POST", "/toy/letter/resend", {}, {})
    )
    second_retry = asyncio.run(
        local_server.route("POST", "/toy/letter/resend", {}, {})
    )

    assert failed["code"] == 503
    assert failed["data"]["error_code"] == "LLM_TIMEOUT"
    assert failed["data"]["retryable"] is True
    assert first_retry["code"] == second_retry["code"] == 501
    assert first_retry["data"]["status"] == "NOT_IMPLEMENTED"
    assert first_retry["data"]["error_code"] == "LETTER_RESEND_NOT_IMPLEMENTED"
    assert len(local_server.store.letters) == 1


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

    assert schema["properties"]["contract_version"]["const"] == "b02.v1"
    assert legacy_schema["properties"]["mode"]["const"] == "read_only"
    assert document["contract_version"] == "b02.v1"
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
        "fields": ["media_status", "media_error_code", "media_retryable"],
        "statuses": [
            "NOT_REQUESTED",
            "PENDING",
            "PROCESSING",
            "COMPLETED",
            "FAILED",
            "UNAVAILABLE",
        ],
        "error_codes": {
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
            "music_duration.py",
            "music_renderer.py",
            "music_reply.py",
            "minimax_profile.py",
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
            return "\n".join(sorted(expected))
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
