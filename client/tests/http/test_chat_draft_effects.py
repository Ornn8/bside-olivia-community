import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from reply_orchestrator import ReplyState
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.personal_chat import backend
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import TURN_IS_CURRENT


@pytest.mark.parametrize('stale', [False, True])
def test_new_input_revision_has_distinct_request_id_and_stale_text_skips_tts(monkeypatch, tmp_path, stale):
    import persona_loader
    from runtime import image_reply
    from runtime.personal_chat import stickers
    from tests.http.test_personal_chat_decision import envelope

    monkeypatch.setattr(persona_loader, 'load_persona', lambda _: SimpleNamespace(snapshot=SimpleNamespace(status='READY')))
    monkeypatch.setattr(image_reply, 'photo_reply_context', lambda context, *args, **kwargs: context)
    monkeypatch.setattr(stickers, 'choices', lambda *args, **kwargs: {})
    seen, speech = [], []
    async def run(request, context):
        seen.append(request.idempotency_key)
        return SimpleNamespace(state=ReplyState.COMPLETED, text=envelope(text='现在醒着就继续聊', delivery='voice'))
    async def audio(server, text, path, **kwargs):
        speech.append(text)
        return {'duration_seconds': 4}
    monkeypatch.setattr(backend, 'prepare_chat_audio', audio)
    server = SimpleNamespace(letters_adapter=SimpleNamespace(config=SimpleNamespace(persona_v2_enabled=True,
        max_input_chars=50000), persona_v2_path='synthetic', build_reply_context=lambda *a, **k: ReplyContext.create(ReplyMode.FUTURE_IM, trusted_time=TrustedTime(datetime.now(timezone.utc)), future_im_enabled=True)),
        _llm_runtime_ready=lambda _: True, daily_life_runtime=object(), _official_history_private_world_available=lambda: True,
        MEMORY_READY_REPLY_TIMEOUT_SECONDS=1, _conversation_memory_ready_for_reply=lambda: True,
        video_reply_settings_store=SimpleNamespace(image_snapshot=lambda: {'enabled': False}),
        _persist_store_state=lambda: None, _state_root=lambda: tmp_path,
        _CURRENT_LETTER_MEMORY_SOURCE=ContextVar('draft_source'), _CURRENT_LETTER_RECEIPT=ContextVar('draft_receipt'),
        store=SimpleNamespace(personal_chats=[]), _voice_reply_configured=lambda _: True,
        supports_scoped_reasoning=lambda _: False, _reply_pipeline_timeout_seconds=lambda _: 1,
        reply_pipeline=SimpleNamespace(run=run))
    async def scenario():
        token = TURN_IS_CURRENT.set(lambda: not stale)
        try:
            for revision in (0, 1):
                row = dict(channel='qq', life_received_at=datetime.now(timezone.utc).isoformat(),
                           input_revision=revision, generation_attempts=1, voice_available=True)
                assert await backend.generate(server, PersonalMessage('qq', '100', '200', '1', '继续聊'), row)
                assert bool(row.get('prepared_audio')) is (not stale)
        finally:
            TURN_IS_CURRENT.reset(token)
    asyncio.run(scenario())
    assert len(set(seen)) == 2
    assert len(speech) == (0 if stale else 2)


def test_shadow_result_of_discarded_revision_cannot_overwrite_new_revision(monkeypatch):
    async def scenario():
        saved = []
        async def persist(server):
            saved.append(True)
        monkeypatch.setattr(backend, 'persist_chat', persist)
        row = {'input_revision': 0, 'generation_attempts': 1}
        result = asyncio.get_running_loop().create_future()
        server = SimpleNamespace()
        backend._start_semantic_shadow_recorder(server, row, result)
        row['input_revision'] = 1
        result.set_result({'status': 'old-result'})
        await asyncio.gather(*tuple(server._semantic_shadow_tasks))
        assert not saved and 'semantic_shadow' not in row
    asyncio.run(scenario())
