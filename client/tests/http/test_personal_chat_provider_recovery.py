"""Provider failures across the real pipeline, durable chat loop and export.

All provider responses are synthetic; no HTTP request or real credential is used.
"""
import asyncio
from contextvars import ContextVar
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from llm_gateway import GatewayConfig, GatewayError, GatewayRequestScope, GatewayResponse
from memory_port import NullMemoryPort
from reply_orchestrator import ReplyOrchestrator
from reply_pipeline import ReplyPipeline, UnavailableRewriter
from reply_reviewer import NullReviewer
from runtime.personal_chat import backend
from runtime.personal_chat.events import PersonalMessage
from runtime.diagnostics.support_bundle import project_chat_task
from tests.http.test_personal_chat_decision import envelope


ROOT = Path(__file__).resolve().parents[2]
TRACE = str(uuid.uuid4())


def _fixture(tmp_path, monkeypatch, failures):
    import local_server
    from runtime.personal_chat import qq
    from runtime.memory import history_selection

    monkeypatch.delenv("OLIVIA_PERSONAL_CHAT_CONFIG", raising=False)
    monkeypatch.setenv("OLIVIA_PERSONAL_QQ_TOKEN", "synthetic-token-123456789")
    monkeypatch.delenv("OLIVIA_COMPANION_JEV_ENABLED", raising=False)
    folder = tmp_path / "personal-chat"
    folder.mkdir()
    (folder / "config.json").write_text(json.dumps({
        "qq": {"url": "ws://127.0.0.1:3001", "account": "100", "owner": "200"},
    }), encoding="utf-8")
    (folder / "existing-access.json").write_text(json.dumps({"channels": ["qq"]}), encoding="utf-8")
    calls, sent, committed, persisted, captured = [], [], [], [], {}

    class Provider:
        stream_enabled = False

        async def complete(self, messages, *, request_id=None):
            calls.append(request_id)
            index = len(calls) - 1
            if index < len(failures) and failures[index] is not None:
                code, retryable, status = failures[index]
                exc = GatewayError(code, retryable=retryable, status=status)
                exc.provider_request_id = TRACE
                exc.diagnostic_stage = "http_response"
                # The failure must not copy arbitrary exception/provider data.
                exc.private_response = "private-provider-body synthetic-token-123456789"
                raise exc
            return GatewayResponse(text=envelope(text=f"synthetic reply {len(calls)}"),
                request_id=request_id or "synthetic", provider="synthetic", model="synthetic")

    adapter = local_server.LetterAdapter(GatewayConfig(
        provider="openai_compatible", base_url="http://127.0.0.1:9/v1", model="synthetic",
        persona_v2_enabled=True, persona_v2_file=str(ROOT / "linli_character/persona_release_v2.json")),
        memory_port=NullMemoryPort())
    adapter.gateway = Provider()
    monkeypatch.setattr(adapter, "_build_memory_prompt", lambda *a, **k: SimpleNamespace(text="", references=()))
    monkeypatch.setattr(adapter, "daily_life_fragments", lambda text: ())
    async def synthetic_history(messages, gateway, **kwargs):
        # These tests isolate writer failure/recovery from semantic recall calls.
        return messages
    monkeypatch.setattr(history_selection, "select_history_messages", synthetic_history)
    pipeline = ReplyPipeline(ReplyOrchestrator(local_server._LetterGateway(adapter), timeout_seconds=1),
        reviewer=NullReviewer(), rewriter=UnavailableRewriter())
    server = SimpleNamespace(letters_adapter=adapter, reply_pipeline=pipeline,
        _llm_runtime_ready=lambda config: True, daily_life_runtime=object(),
        _official_history_private_world_available=lambda: True,
        MEMORY_READY_REPLY_TIMEOUT_SECONDS=.1, _conversation_memory_ready_for_reply=lambda: True,
        _CURRENT_LETTER_MEMORY_SOURCE=ContextVar("provider_recovery_source", default=None),
        _CURRENT_LETTER_RECEIPT=ContextVar("provider_recovery_receipt", default=None),
        GatewayRequestScope=GatewayRequestScope, supports_scoped_reasoning=lambda config: False,
        _reply_pipeline_timeout_seconds=lambda mode: 2, store=local_server.Store(),
        video_reply_settings_store=SimpleNamespace(image_snapshot=lambda: {"enabled": False}),
        _state_root=lambda: tmp_path, _require_store_state_available=lambda: None,
        _safe_log=lambda *a, **k: None)

    def persist():
        persisted.append(json.loads(json.dumps(server.store.personal_chats)))
    server._persist_store_state = persist

    async def commit(server, row):
        committed.append(row["letter_id"])

    async def listen(url, token, account, owner, handler, stop, **kwargs):
        captured["handler"] = handler
        kwargs["state_callback"]("CONNECTED")
        await stop.wait()

    async def send(text):
        sent.append(text)
        return "synthetic-ack"

    monkeypatch.setattr(backend, "recoverable_commit", commit)
    monkeypatch.setattr(qq, "run_qq", listen)

    async def boot():
        captured.clear()
        app = web.Application()
        backend.install_personal_chat(app, server)
        runner = web.AppRunner(app)
        await runner.setup()
        for _ in range(100):
            if "handler" in captured:
                break
            await asyncio.sleep(0)
        assert "handler" in captured
        return app, runner

    return server, calls, sent, committed, persisted, captured, send, boot


@pytest.mark.parametrize("code,status", [
    ("PROVIDER_USAGE_PENDING", 502),
    ("PROVIDER_REQUEST_DUPLICATE", 409),
    ("PROVIDER_AUTH_FAILED", 401),
    ("PROVIDER_QUOTA_EXHAUSTED", 429),
])
def test_terminal_provider_failure_is_not_regenerated_on_retry_or_restart(tmp_path, monkeypatch, code, status):
    failures = [(code, False, status)] * 2
    fixture = _fixture(tmp_path, monkeypatch, failures)
    server, calls, sent, committed, persisted, captured, send, boot = fixture

    async def scenario():
        failed_event = PersonalMessage("qq", "100", "200", "failed", "private-user-message")
        app, runner = await boot()
        try:
            await captured["handler"](failed_event, send)
            row = server.store.personal_chats[0]
            assert len(calls) == row["generation_attempts"] == 1
            assert row["delivery_status"] == "FAILED"
            assert row["error_code"] == "PERSONAL_CHAT_" + code
            assert len(sent) == 1 and row["generation_failure_notice"] == "DELIVERED"
            assert committed == [] and not row.get("reply_text")
            await captured["handler"](failed_event, send)
            assert len(calls) == len(sent) == 1
        finally:
            await runner.cleanup()

        # Restart from only JSON-persisted rows, discarding process-local state.
        assert persisted[-1][0]["generation_attempts"] == 1
        server.store.personal_chats = json.loads(json.dumps(persisted[-1]))
        app, runner = await boot()
        try:
            await captured["handler"](failed_event, send)
            assert len(calls) == len(sent) == 1
            # The next input still works after the provider becomes available.
            failures.clear()
            good = PersonalMessage("qq", "100", "200", "next", "new-user-message")
            await captured["handler"](good, send)
            await asyncio.sleep(0)
            assert len(calls) == 2 and sent[-1] == "synthetic reply 2"
            assert server.store.personal_chats[-1]["delivery_status"] == "DELIVERED"
            assert committed == [good.exchange_id]
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


@pytest.mark.parametrize("recover", [False, True])
def test_transient_provider_retry_is_bounded_and_success_is_delivered(tmp_path, monkeypatch, recover):
    failures = [("PROVIDER_RETRYABLE", True, 503), None if recover else ("PROVIDER_RETRYABLE", True, 503)]
    server, calls, sent, committed, persisted, captured, send, boot = _fixture(tmp_path, monkeypatch, failures)

    async def scenario():
        app, runner = await boot()
        try:
            event = PersonalMessage("qq", "100", "200", "transient", "private-user-message")
            await captured["handler"](event, send)
            await asyncio.sleep(0)
            row = server.store.personal_chats[0]
            assert len(calls) == row["generation_attempts"] == 2
            assert row["delivery_status"] == ("DELIVERED" if recover else "FAILED")
            assert len(sent) == 1
            if recover:
                assert sent == ["synthetic reply 2"] and committed == [event.exchange_id]
            else:
                assert committed == [] and row["generation_failure_notice"] == "DELIVERED"
            await captured["handler"](event, send)
            assert len(calls) == 2 and len(sent) == 1
            good = PersonalMessage("qq", "100", "200", "next", "new-user-message")
            await captured["handler"](good, send)
            await asyncio.sleep(0)
            assert len(calls) == 3 and sent[-1] == "synthetic reply 3"
            assert server.store.personal_chats[-1]["delivery_status"] == "DELIVERED"
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


def test_provider_failure_context_is_durable_and_bound_to_sanitized_turn(tmp_path, monkeypatch):
    server, calls, sent, committed, persisted, captured, send, boot = _fixture(tmp_path, monkeypatch,
        [("PROVIDER_USAGE_PENDING", False, 502)] * 2)

    async def scenario():
        app, runner = await boot()
        try:
            await captured["handler"](PersonalMessage("qq", "100", "200", "failed", "private-user-message"), send)
        finally:
            await runner.cleanup()
    asyncio.run(scenario())
    row = json.loads(json.dumps(persisted[-1][0]))
    projected = project_chat_task(row)
    context = projected.get("generation_failure_context", {})
    assert context.items() >= {
        "provider_code": "PROVIDER_USAGE_PENDING", "http_status": 502,
        "failure_stage": "http_response", "provider_request_id": TRACE,
    }.items()
    assert projected["turn_ref"].startswith("chat-")
    assert projected["generation_attempts"] == 1
    assert projected.get("generation_retryable") is False
    exported = json.dumps(projected)
    assert "private-" not in exported and "synthetic-token" not in exported


def test_malformed_failure_metadata_cannot_export_private_payload():
    projected = project_chat_task({"channel": "qq", "generation_retryable": "private-key",
        "generation_failure_context": {"provider_request_id": "private-account",
            "provider_code": "private-body", "http_status": True,
            "failure_stage": "private-stage", "message": "private-user-message",
            "exception": "synthetic-token-123456789"}})
    exported = json.dumps(projected)
    assert "private-" not in exported and "synthetic-token" not in exported


def test_local_context_failure_is_not_retried_and_keeps_safe_diagnostic(tmp_path, monkeypatch):
    from runtime.reply import companion_runtime
    from tests.persona.test_jev_pipeline import Port
    server, calls, sent, committed, persisted, captured, send, boot = _fixture(tmp_path, monkeypatch, [])
    server.reply_pipeline.companion_decision_port = Port()
    context_calls = []
    original = companion_runtime._decision_context

    def missing_source(messages, required=(), recent_turns=1):
        context_calls.append(True)
        return original(messages, ['reply:private-missing:user'], recent_turns)

    monkeypatch.setattr(companion_runtime, '_decision_context', missing_source)

    async def scenario():
        event = PersonalMessage('qq', '100', '200', 'missing-context', 'private-user-message')
        for _ in range(2):
            _, runner = await boot()
            try:
                await captured['handler'](event, send)
            finally:
                await runner.cleanup()

    asyncio.run(scenario())
    assert len(context_calls) == 1 and not calls and not committed and len(sent) == 1
    exported = project_chat_task(json.loads(json.dumps(persisted[-1][0])))
    assert exported['generation_attempts'] == 1 and exported['generation_retryable'] is False
    assert persisted[-1][0]['error_code'] == 'JEV_CONTEXT_UNAVAILABLE'
    assert exported['generation_failure_context'] == dict(failure_stage='decision_context',
                                                          failure_detail='required_source_missing')
    assert 'private-' not in json.dumps(exported)


def test_usage_pending_loopback_gateway_is_not_resubmitted_by_chat_loop(tmp_path, monkeypatch):
    from llm_gateway import OpenAICompatibleAdapter
    server, calls, sent, committed, persisted, captured, send, boot = _fixture(tmp_path, monkeypatch, [])
    http_calls = []

    async def scenario():
        async def provider(request):
            http_calls.append((await request.json())["model"])
            return web.json_response({"error": {"code": "upstream_unavailable_usage_pending",
                "message": "private-provider-body synthetic-token-123456789"}}, status=502,
                headers={"X-Request-ID": TRACE})
        provider_app = web.Application()
        provider_app.router.add_post("/v1/chat/completions", provider)
        async with TestClient(TestServer(provider_app)) as client:
            config = GatewayConfig(provider="openai_compatible", base_url=str(client.make_url("/v1")),
                model="synthetic", requires_api_key=False, max_retries=2)
            server.letters_adapter.gateway = OpenAICompatibleAdapter(config)
            app, runner = await boot()
            try:
                event = PersonalMessage("qq", "100", "200", "failed", "private-user-message")
                await captured["handler"](event, send)
                row = server.store.personal_chats[0]
                assert http_calls == ["synthetic"]
                assert row["generation_attempts"] == 1 and row["delivery_status"] == "FAILED"
                assert row["error_code"] == "PERSONAL_CHAT_PROVIDER_USAGE_PENDING"
                await captured["handler"](event, send)
                assert http_calls == ["synthetic"] and len(sent) == 1
                safe = project_chat_task(json.loads(json.dumps(persisted[-1][0])))
                context = safe.get("generation_failure_context", {})
                assert context.get("provider_request_id") == TRACE
                assert context.get("http_status") == 502
                assert context.get("failure_stage") == "http_response"
                assert "private-" not in json.dumps(safe)
            finally:
                await runner.cleanup()
    asyncio.run(scenario())


@pytest.mark.parametrize('headroom', [0, 512])
def test_full_chat_context_reaches_writer_without_losing_recall(tmp_path, monkeypatch, headroom):
    from runtime.memory import history_selection
    from runtime.personal_chat.decision import INSTRUCTION

    server, calls, sent, committed, persisted, captured, send, boot = _fixture(tmp_path, monkeypatch, [])
    selected, authored = [], []
    provider = server.letters_adapter.gateway
    complete = provider.complete

    async def record_request(messages, *, request_id=None):
        authored.append(messages)
        return await complete(messages, request_id=request_id)

    async def full_history(messages, gateway, *, max_input_chars, **kwargs):
        # Simulate a large archive filling its allowed recall budget. Keep the
        # real persona/time/output assembly and durable QQ send path in use.
        result = [dict(m) for m in messages]
        result.insert(-1, {'role': 'assistant', 'content': '[历史消息 {}]\n合成的已确认约定'})
        remaining = max_input_chars - sum(len(m['content']) for m in result) - headroom
        assert remaining > 0
        result[0]['content'] += '忆' * remaining
        selected.append((result, max_input_chars))
        return tuple(result)

    monkeypatch.setattr(provider, 'complete', record_request)
    monkeypatch.setattr(history_selection, 'select_history_messages', full_history)

    async def scenario():
        app, runner = await boot()
        try:
            event = PersonalMessage('qq', '100', '200', 'large-context', '吃完记得休息')
            await captured['handler'](event, send)
            await asyncio.sleep(0)
            row = server.store.personal_chats[0]
            assert row['delivery_status'] == 'DELIVERED', row.get('error_code')
            assert len(calls) == 1 and sent == ['synthetic reply 1']
            assert committed == [event.exchange_id]
            assert row.get('generation_failure_notice') != 'DELIVERED'
        finally:
            await runner.cleanup()
    asyncio.run(scenario())
    before, recall_limit = selected[0]
    after = authored[0]
    from runtime.reply.context_budget import wire_size
    assert sum(len(m['content']) for m in after) <= 100000
    assert wire_size(after) <= 88000
    assert after[-1] == before[-1]
    assert sum(m['content'].count('忆') for m in after) == sum(m['content'].count('忆') for m in before)
    assert sum(m['content'].count(INSTRUCTION) for m in after) == 1
    assert [m for m in after if m['role'] == 'assistant'] == [m for m in before if m['role'] == 'assistant']
    assert '<chat_output_rules>' in after[0]['content']
