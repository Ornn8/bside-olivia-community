"""Synthetic integration through the native store, adapter and reply pipeline."""
import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_gateway import GatewayConfig, GatewayResponse, GatewayRequestScope
from memory_port import NullMemoryPort
from runtime.personal_chat import backend
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService
from reply_context import ReplyMode, ReplyContextError
from reply_orchestrator import ReplyOrchestrator
from reply_pipeline import ReplyPipeline, UnavailableRewriter
from reply_reviewer import NullReviewer
from persona_assembly import UntrustedFragment

ROOT = Path(__file__).resolve().parents[2]


def test_native_adapter_im_requires_explicit_opt_in_and_preserves_projection():
    import local_server
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    adapter = local_server.LetterAdapter(GatewayConfig(), memory_port=NullMemoryPort(), now=lambda: now)
    with pytest.raises(ReplyContextError):
        adapter.build_reply_context(ReplyMode.FUTURE_IM)
    letter = adapter.build_reply_context(ReplyMode.TEXT_LETTER)
    chat = adapter.build_reply_context(ReplyMode.FUTURE_IM, future_im_enabled=True)
    assert chat.mode == ReplyMode.FUTURE_IM
    assert chat.trusted_time == letter.trusted_time
    assert chat.private_behavior == letter.private_behavior
    assert chat.world_facts == letter.world_facts


def test_native_store_roundtrip_keeps_chat_outside_inbox(tmp_path, monkeypatch):
    import local_server
    native = local_server.Store()
    native.letters.append({"letter_id": "native-letter", "content": "native inbox", "reply_mode": "text_letter"})
    chat = {"letter_id": "im-synthetic", "content": "personal chat", "reply_mode": "future_im",
            "delivery_status": "SENDING", "letter_status": "PROCESSING"}
    native.personal_chats.append(chat)
    monkeypatch.setattr(local_server, "store", native)
    monkeypatch.setattr(local_server, "_state_root", lambda: tmp_path)
    monkeypatch.setattr(local_server, "_store_state_error_code", None)
    local_server._persist_store_state()
    serialized = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert serialized["personal_chats"] == [chat]
    assert [row["letter_id"] for row in serialized["letters"]] == ["native-letter"]
    reloaded = local_server.Store()
    monkeypatch.setattr(local_server, "store", reloaded)
    local_server._load_store_state()
    assert reloaded.personal_chats == [chat]
    assert reloaded.personal_chats[0]["delivery_status"] == "SENDING"
    assert [row["letter_id"] for row in reloaded.letters] == ["native-letter"]


def test_backend_generate_uses_real_pipeline_persona_memory_and_world(monkeypatch):
    import local_server
    calls = []
    class Provider:
        stream_enabled = False
        async def complete(self, messages, *, request_id=None):
            calls.append(tuple(dict(m) for m in messages))
            return GatewayResponse(text="那你明天再跟我说嘛。", request_id=request_id or "test",
                                   provider="synthetic", model="synthetic")
    adapter = local_server.LetterAdapter(GatewayConfig(provider="openai_compatible",
        base_url="http://127.0.0.1:9/v1", model="synthetic", persona_v2_enabled=True,
        persona_v2_file=str(ROOT / "linli_character/persona_release_v2.json")), memory_port=NullMemoryPort())
    adapter.gateway = Provider()
    monkeypatch.setattr(adapter, "_build_memory_prompt", lambda *a, **k: SimpleNamespace(text="synthetic-memory-keeps-piano", references=()))
    monkeypatch.setattr(adapter, "daily_life_fragments", lambda text: (UntrustedFragment("life.synthetic", "synthetic-world-evening-piano"),))
    pipeline = ReplyPipeline(ReplyOrchestrator(local_server._LetterGateway(adapter), timeout_seconds=1),
        reviewer=NullReviewer(), rewriter=UnavailableRewriter())
    source = ContextVar("test_personal_source", default="previous-source")
    receipt = ContextVar("test_personal_receipt", default=None)
    server = SimpleNamespace(letters_adapter=adapter, reply_pipeline=pipeline,
        _llm_runtime_ready=lambda config: True, daily_life_runtime=object(),
        _official_history_private_world_available=lambda: True,
        MEMORY_READY_REPLY_TIMEOUT_SECONDS=.1, _conversation_memory_ready_for_reply=lambda: True,
        _CURRENT_LETTER_MEMORY_SOURCE=source, _CURRENT_LETTER_RECEIPT=receipt,
        GatewayRequestScope=GatewayRequestScope, supports_scoped_reasoning=lambda config: False,
        _reply_pipeline_timeout_seconds=lambda mode: 2)
    event = PersonalMessage("qq", "100", "200", "1", "今天钢琴练得怎么样？")
    async def scenario():
        result = await backend.generate(server, event, {"life_received_at": datetime.now(timezone.utc).isoformat()})
        assert source.get() == "previous-source" and receipt.get() is None
        return result
    assert asyncio.run(scenario()) == "那你明天再跟我说嘛。"
    assert len(calls) == 1
    system = calls[0][0]["content"]
    assert "constitution" in system and "future_im" in system
    assert "即时聊天" in system
    assert "synthetic-memory-keeps-piano" in system
    assert "synthetic-world-evening-piano" in system
    assert calls[0][-1]["content"] == event.text


def test_service_ack_precedes_backend_world_and_daily_life_commit():
    async def scenario():
        operations = []
        rows = []
        server = SimpleNamespace(daily_life_tasks={},
            private_world_candidate_store=None,
            _persist_store_state=lambda: operations.append("persist"),
            _schedule_private_world_candidate=lambda *args: operations.append("candidate"),
            letters_adapter=SimpleNamespace(remember_conversation=lambda *args: operations.append("memory")))
        def world(row):
            assert row["delivery_status"] == "DELIVERED"
            operations.append("world")
            row["private_world_status"] = "COMMITTED"
            return True
        def daily(row):
            async def consume():
                assert row["delivery_status"] == "DELIVERED"
                operations.append("daily")
                row["daily_life_status"] = "COMMITTED"
            server.daily_life_tasks[f"reply:{row['letter_id']}:1"] = asyncio.create_task(consume())
        server._commit_private_world_letter = world
        server._schedule_daily_life_exchange = daily
        async def generate(event, row):
            operations.append("generate")
            return "synthetic reply"
        async def send(text):
            assert rows[0]["delivery_status"] == "SENDING"
            assert not any(stage in operations for stage in ("world", "daily", "memory"))
            operations.append("ack")
            return "confirmed-platform-id"
        service = PersonalChatService(rows, server._persist_store_state, generate,
            lambda row: backend.commit(server, row), {"qq": ("100", "200")})
        await service.handle(PersonalMessage("qq", "100", "200", "1", "hello"), send)
        assert operations.index("ack") < operations.index("world") < operations.index("daily") < operations.index("memory")
        await service.recover()
        assert operations.count("world") == operations.count("daily") == operations.count("memory") == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["GENERATING", "GENERATED", "SENDING", "FAILED"])
def test_backend_never_commits_undelivered_reply(status):
    # No server methods exist: touching any consumer before ACK fails this test.
    asyncio.run(backend.commit(SimpleNamespace(), {"delivery_status": status,
        "private_world_status": "PENDING", "daily_life_status": "PENDING"}))


@pytest.mark.parametrize("unavailable", ["persona", "world", "memory"])
def test_backend_readiness_never_bypasses_required_systems(monkeypatch, unavailable):
    import persona_loader
    monkeypatch.setattr(persona_loader, "load_persona", lambda path: SimpleNamespace(snapshot=SimpleNamespace(status="READY")))
    server = SimpleNamespace(letters_adapter=SimpleNamespace(config=SimpleNamespace(persona_v2_enabled=unavailable != "persona"),
        persona_v2_path=Path("synthetic-only")), _llm_runtime_ready=lambda config: True,
        daily_life_runtime=None if unavailable == "world" else object(),
        _official_history_private_world_available=lambda: True,
        MEMORY_READY_REPLY_TIMEOUT_SECONDS=0, _conversation_memory_ready_for_reply=lambda: False)
    # Intentionally no pipeline/source context: none may be accessed on failure.
    with pytest.raises(RuntimeError, match="PERSONAL_CHAT_" + unavailable.upper() + "_UNAVAILABLE"):
        asyncio.run(backend.generate(server, PersonalMessage("qq", "100", "200", "1", "hello"), {}))


def test_world_commit_failure_preserves_pending_and_does_not_advance_other_consumers():
    operations = []
    server = SimpleNamespace(_commit_private_world_letter=lambda row: False,
        _persist_store_state=lambda: operations.append("persist"))
    row = {"delivery_status": "DELIVERED", "private_world_status": "PENDING", "daily_life_status": "PENDING"}
    with pytest.raises(RuntimeError, match="PERSONAL_CHAT_WORLD_COMMIT_UNAVAILABLE"):
        asyncio.run(backend.commit(server, row))
    assert row["private_world_status"] == row["daily_life_status"] == "PENDING"
    assert operations == ["persist"]


def test_candidate_recovers_after_ledger_commit_without_duplicate_ledger_write():
    async def scenario():
        calls = []
        async def candidate(row, user, reply):
            calls.append("candidate")
            return SimpleNamespace(value="SKIPPED")
        server = SimpleNamespace(private_world_candidate_store=object(),
            _deliver_private_world_candidate=candidate,
            _persist_store_state=lambda: calls.append("persist"))
        row = {"delivery_status": "DELIVERED", "private_world_status": "COMMITTED",
               "daily_life_status": "COMMITTED", "legacy_memory_delivered": True,
               "content": "synthetic", "reply_text": "reply"}
        await backend.commit(server, row)
        await backend.commit(server, row)
        assert calls == ["candidate", "persist"]
    asyncio.run(scenario())


def test_two_channel_lifecycle_uses_one_service_and_stops_owned_tasks(tmp_path, monkeypatch):
    from aiohttp import web
    from runtime.personal_chat import qq, wechat
    import original_client_setup_api
    import local_server
    config = tmp_path / "channels.json"
    credentials = tmp_path / "wechat.dpapi"
    credentials.write_text(json.dumps({"account": "bot", "owner": "owner"}), encoding="utf-8")
    config.write_text(json.dumps({"wechat": {"credentials_file": str(credentials)},
        "qq": {"url": "ws://127.0.0.1:3001", "account": "100", "owner": "200"}}), encoding="utf-8")
    monkeypatch.setenv("OLIVIA_PERSONAL_CHAT_CONFIG", str(config))
    monkeypatch.setenv("OLIVIA_PERSONAL_QQ_TOKEN", "synthetic-token-123456789")
    monkeypatch.setattr(original_client_setup_api, "_dpapi_unprotect", lambda value: value)
    async def scenario():
        seen, stopped = [], []
        async def generate(server, event, row):
            seen.append(event.channel)
            await asyncio.sleep(0)
            return "synthetic reply"
        async def commit(server, row):
            assert row["delivery_status"] == "DELIVERED"
        async def sender(text):
            return "receipt"
        async def run_wechat(credentials, handler, stop, **kwargs):
            try:
                await handler(PersonalMessage("wechat", "bot", "owner", "1", "wechat text"), sender)
                kwargs["cursor_store"].save("synthetic-cursor")
                await stop.wait()
            finally:
                stopped.append("wechat")
        async def run_qq(url, token, account, owner, handler, stop):
            try:
                await handler(PersonalMessage("qq", account, owner, "probe", "/连接测试"), sender)
                await handler(PersonalMessage("qq", account, owner, "probe", "/连接测试"), sender)
                await handler(PersonalMessage("qq", account, owner, "1", "qq text"), sender)
                await stop.wait()
            finally:
                stopped.append("qq")
        monkeypatch.setattr(backend, "generate", generate)
        monkeypatch.setattr(backend, "commit", commit)
        monkeypatch.setattr(wechat, "run_wechat", run_wechat)
        monkeypatch.setattr(qq, "run_qq", run_qq)
        server = SimpleNamespace(store=local_server.Store(), _state_root=lambda: tmp_path,
            _require_store_state_available=lambda: None, _persist_store_state=lambda: None,
            _safe_log=lambda *a, **k: None)
        app = web.Application()
        backend.install_personal_chat(app, server)
        runner = web.AppRunner(app)
        await runner.setup()
        for _ in range(20):
            await asyncio.sleep(.005)
            if len(server.store.personal_chats) == 2 and all(r.get("delivery_status") == "DELIVERED" for r in server.store.personal_chats):
                break
        await runner.cleanup()
        assert sorted(seen) == ["qq", "wechat"]
        assert sorted(stopped) == ["qq", "wechat"]
        assert server.store.letters == [] and len(server.store.personal_chats) == 2
        assert server.store.settings == {} and list(server.store.personal_chat_cursors.values()) == ["synthetic-cursor"]
        assert app[backend._RUNTIME]["roundtrips"] == {"qq": 1}
    asyncio.run(scenario())


def test_diagnostic_snapshot_excludes_content_and_secrets(tmp_path):
    import local_server
    server = SimpleNamespace(store=local_server.Store(), _state_root=lambda: tmp_path,
        _atomic_write_store_file=local_server._atomic_write_store_file)
    server.store.personal_chats = [{"delivery_status": "SENDING", "content": "private-text", "owner": "private-owner"}]
    runtime = {"status": {"wechat": "FAILED"}, "errors": {"wechat": backend._failure_code(RuntimeError("secret-token=https://private"))}}
    backend._publish_status(server, runtime)
    text = (tmp_path / "personal-chat-status.json").read_text(encoding="utf-8")
    assert "private" not in text and "secret" not in text
    assert json.loads(text)["exchanges"] == {"SENDING": 1}
    assert backend._failure_code(RuntimeError("WECHAT_API_REJECTED")) == "WECHAT_API_REJECTED"


def test_saved_config_is_loaded_on_normal_start_without_environment(tmp_path, monkeypatch):
    from aiohttp import web
    from runtime.personal_chat import qq
    import local_server, original_client_setup_api
    monkeypatch.delenv("OLIVIA_PERSONAL_CHAT_CONFIG", raising=False)
    monkeypatch.delenv("OLIVIA_PERSONAL_QQ_TOKEN", raising=False)
    secret = tmp_path / "qq.dpapi"
    secret.write_text(json.dumps({"token": "synthetic-token-123456789"}), encoding="utf-8")
    folder = tmp_path / "personal-chat"
    folder.mkdir()
    (folder / "config.json").write_text(json.dumps({"qq": {"url": "ws://127.0.0.1:3001", "account": "100", "owner": "200", "credentials_file": str(secret)}}), encoding="utf-8")
    monkeypatch.setattr(original_client_setup_api, "_dpapi_unprotect", lambda x: x)
    async def scenario():
        called = asyncio.Event()
        async def run(url, token, account, owner, handle, stop):
            assert token == "synthetic-token-123456789" and account == "100" and owner == "200"
            called.set()
            await stop.wait()
        monkeypatch.setattr(qq, "run_qq", run)
        server = SimpleNamespace(store=local_server.Store(), _state_root=lambda: tmp_path,
            _require_store_state_available=lambda: None, _persist_store_state=lambda: None,
            _safe_log=lambda *a, **k: None)
        app = web.Application()
        backend.install_personal_chat(app, server)
        runner = web.AppRunner(app)
        await runner.setup()
        await asyncio.wait_for(called.wait(), 2)
        await runner.cleanup()
    asyncio.run(scenario())


def test_consumer_failure_preserves_reply_and_retries_without_stopping_transport(monkeypatch):
    async def scenario():
        attempts, sends = [], []
        async def fail(server, row):
            attempts.append(row["letter_id"])
            raise RuntimeError("PERSONAL_CHAT_DAILY_LIFE_UNAVAILABLE")
        monkeypatch.setattr(backend, "commit", fail)
        server = SimpleNamespace(_persist_store_state=lambda: None, _safe_log=lambda *a, **k: None)
        async def generate(event, row):
            return "reply"
        async def send(text):
            sends.append(text)
        rows = []
        service = PersonalChatService(rows, lambda: None, generate,
            lambda row: backend.recoverable_commit(server, row), {"qq": ("100", "200")})
        await service.handle(PersonalMessage("qq", "100", "200", "1", "hello"), send)
        assert rows[0]["delivery_status"] == "DELIVERED" and rows[0]["consumer_failures"] == 1
        for _ in range(5):
            await service.recover()
        assert len(attempts) == 3 and sends == ["reply"]
        assert rows[0]["consumer_error_code"] == "PERSONAL_CHAT_DAILY_LIFE_UNAVAILABLE"
    asyncio.run(scenario())
