"""Synthetic transport boundaries for one frozen Jev decision per input revision."""
import asyncio
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from reply_orchestrator import ReplyState
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.personal_chat import backend
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService, TURN_IS_CURRENT, _clear_draft
from tests.http.test_personal_chat_decision import envelope


RECORD = {'input_digest': 'synthetic-digest', 'input_revision': 0,
          'source_ids': ['synthetic-user'], 'decision': {'synthetic': True}}


def server_fixture(monkeypatch, tmp_path, result, *, run_hook=None):
    import persona_loader
    import runtime.image_reply
    import runtime.image_understanding
    import runtime.personal_chat.stickers
    from runtime.personal_chat.presentation import CURRENT

    monkeypatch.setattr(persona_loader, 'load_persona', lambda _: SimpleNamespace(snapshot=SimpleNamespace(status='READY')))
    monkeypatch.setattr(runtime.image_reply, 'photo_reply_context', lambda ctx, *a, **k: ctx)
    monkeypatch.setattr(runtime.personal_chat.stickers, 'choices', lambda *a, **k: {})
    async def understand(*a): pass
    monkeypatch.setattr(runtime.image_understanding, 'understand_incoming', understand)
    seen, saved, audio = [], [], []
    async def run(request, context):
        seen.append(deepcopy(CURRENT.get()))
        if run_hook:
            await run_hook()
        return result
    async def prepare(server, text, path, **kwargs):
        audio.append(text)
        return {'duration_seconds': 2}
    monkeypatch.setattr(backend, 'prepare_chat_audio', prepare)
    row = {'channel': 'qq', 'life_received_at': datetime.now(timezone.utc).isoformat(),
           'voice_available': True, 'input_revision': 0}
    server = SimpleNamespace(letters_adapter=SimpleNamespace(config=SimpleNamespace(persona_v2_enabled=True,
        max_input_chars=50000), persona_v2_path='synthetic', build_reply_context=lambda *a, **k: ReplyContext.create(ReplyMode.FUTURE_IM, trusted_time=TrustedTime(datetime.now(timezone.utc)), future_im_enabled=True)),
        _llm_runtime_ready=lambda _: True, daily_life_runtime=object(), _official_history_private_world_available=lambda: True,
        MEMORY_READY_REPLY_TIMEOUT_SECONDS=1, _conversation_memory_ready_for_reply=lambda: True,
        video_reply_settings_store=SimpleNamespace(image_snapshot=lambda: {'enabled': True}),
        _persist_store_state=lambda: saved.append(deepcopy(row)), _state_root=lambda: tmp_path,
        _CURRENT_LETTER_MEMORY_SOURCE=ContextVar('jev_source'), _CURRENT_LETTER_RECEIPT=ContextVar('jev_receipt'),
        store=SimpleNamespace(personal_chats=[row], letters=[]), _voice_reply_configured=lambda _: True,
        supports_scoped_reasoning=lambda _: False, _reply_pipeline_timeout_seconds=lambda _: 1,
        _safe_log=lambda *a, **k: None, reply_pipeline=SimpleNamespace(run=run))
    return server, row, seen, saved, audio


def pipeline_result(*, timing='now', delivery='text', text=None, error=None, record=True):
    return SimpleNamespace(state=ReplyState.FAILED if error else ReplyState.COMPLETED,
        error_code=error, text=envelope(text='Hello', delivery='voice') if text is None else text,
        companion_decision=deepcopy(RECORD) if record else None,
        companion_timing=timing if record else None, companion_delivery=delivery if record else None)


def test_retry_freezes_only_turn_time_and_new_revision_resets_it(monkeypatch, tmp_path):
    from datetime import timedelta
    from runtime.personal_chat.service import _clear_draft
    server, row, *_ = server_fixture(monkeypatch, tmp_path, pipeline_result(record=False))
    now = datetime(2026, 10, 3, 9, tzinfo=timezone.utc)
    clocks = iter((now, now + timedelta(seconds=2), now + timedelta(seconds=4)))
    seen = []
    def build(*args, **kwargs):
        return ReplyContext.create(ReplyMode.FUTURE_IM, trusted_time=TrustedTime(next(clocks)),
                                   future_im_enabled=True)
    server.letters_adapter.build_reply_context = build
    async def run(request, context):
        seen.append(context.trusted_time.instant)
        return pipeline_result(record=False)
    server.reply_pipeline.run = run
    event = PersonalMessage('qq', 'a', 'u', '1', 'Hello')
    async def scenario():
        await backend.generate(server, event, row)
        await backend.generate(server, event, row)
        _clear_draft(row)
        row['input_revision'] = 1
        await backend.generate(server, event, row)
    asyncio.run(scenario())
    assert seen == [now, now, now + timedelta(seconds=4)]


@pytest.mark.parametrize('channel', ['qq', 'wechat'])
@pytest.mark.parametrize('timing', ['now', 'close_turn'])
def test_backend_binds_current_decision_and_overrides_writer_media(monkeypatch, tmp_path, channel, timing):
    server, row, seen, saved, audio = server_fixture(monkeypatch, tmp_path, pipeline_result(timing=timing))
    row['channel'] = channel
    event = PersonalMessage(channel, 'bot', 'owner', '1', 'Hello')
    assert asyncio.run(backend.generate(server, event, row)) == 'Hello'
    assert seen[0]['received_source_id'] == f'reply:{event.exchange_id}:user'
    assert seen[0]['input_revision'] == 0
    assert row['companion_decision'] == RECORD
    assert row['companion_timing'] == timing and row['companion_delivery'] == 'text'
    assert row['requested_format'] == 'text' and audio == []
    assert any(item.get('companion_decision') == RECORD for item in saved)


def test_backend_jev_audio_uses_actual_body_without_new_instructions(monkeypatch, tmp_path):
    server, row, seen, saved, audio = server_fixture(monkeypatch, tmp_path,
        pipeline_result(delivery='audio_speech', text=envelope(text='Only these words', delivery='text')))
    assert asyncio.run(backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', 'Hello'), row)) == 'Only these words'
    assert row['requested_format'] == 'voice' and audio == ['Only these words']


def ordinary_chat_result(**values):
    from tests.persona.test_jev_pipeline import plan
    result = pipeline_result(text=envelope(text='今天练得挺顺，刚歇下来。', **values))
    result.companion_decision['plan'] = plan()
    return result


@pytest.mark.parametrize('listening', ['voice_ok', 'text_only'])
def test_qq_default_speech_does_not_depend_on_writer_opting_in_or_old_listening_preference(
        monkeypatch, tmp_path, listening):
    result = ordinary_chat_result(delivery='text')
    server, row, _, _, audio = server_fixture(monkeypatch, tmp_path, result)
    row['listening_preference'] = listening
    event = PersonalMessage('qq', 'b', 'u', '1', '今天怎么样？')
    assert asyncio.run(backend.generate(server, event, row)) == '今天练得挺顺，刚歇下来。'
    assert row['requested_format'] == 'voice' and audio == ['今天练得挺顺，刚歇下来。']
    assert row['delivery_basis'] == 'QQ_DEFAULT_VOICE'


def test_pending_future_media_does_not_lock_current_qq_reply_to_text(monkeypatch, tmp_path):
    result = ordinary_chat_result(delivery='text')
    result.companion_decision['plan']['understanding']['requirements'] = [dict(
        id='later_photo', fulfillment='pending', alternatives=[dict(kinds=['image'], min_assets=1, max_assets=1)],
        evidence_turn_ids=['t1'])]
    server, row, _, _, audio = server_fixture(monkeypatch, tmp_path, result)
    asyncio.run(backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', '今天怎么样？'), row))
    assert row['requested_format'] == 'voice' and len(audio) == 1


@pytest.mark.parametrize('restriction', ['current_text', 'no_extras', 'uncertain_media'])
def test_jev_current_media_constraints_still_control_delivery(monkeypatch, tmp_path, restriction):
    result = ordinary_chat_result(delivery='voice')
    value = result.companion_decision['plan']
    if restriction in {'current_text', 'no_extras'}:
        value['understanding']['requirements'] = [dict(id='requested_text', fulfillment='current',
            alternatives=[dict(kinds=['text'], min_assets=1, max_assets=1)], evidence_turn_ids=['t1'])]
        value['proposal']['steps'][0]['requirement_ids'] = ['requested_text']
        if restriction == 'no_extras':
            value['understanding']['extras_allowed'] = False
    else:
        value['proposal']['clarify_fields'] = ['media_requirement']
        value['proposal']['moves'][0]['act'] = 'clarify'
        value['resolution'].update(status='needs_clarification', uncertain_fields=['media_requirement'])
    server, row, _, _, audio = server_fixture(monkeypatch, tmp_path, result)
    asyncio.run(backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', '这次发文字'), row))
    assert row['requested_format'] == 'text' and audio == []


@pytest.mark.parametrize('reason', ['speaker_unavailable', 'verbatim_text'])
def test_same_writer_call_can_keep_text_for_speaker_or_literal_content(monkeypatch, tmp_path, reason):
    result = ordinary_chat_result(delivery='text', text_reason=reason)
    server, row, _, _, audio = server_fixture(monkeypatch, tmp_path, result)
    asyncio.run(backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', '今天怎么样？'), row))
    assert row['requested_format'] == 'text' and audio == []
    assert row['delivery_basis'] == reason.upper()


@pytest.mark.parametrize('reason', ['recipient_cannot_listen', 'long_reply', 'recent_voice', {'private': 'annotation'}])
def test_recipient_listening_and_invented_reasons_cannot_suppress_default_speech(monkeypatch, tmp_path, reason):
    result = ordinary_chat_result(delivery='text', text_reason=reason)
    server, row, _, _, audio = server_fixture(monkeypatch, tmp_path, result)
    asyncio.run(backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', '今天怎么样？'), row))
    assert row['requested_format'] == 'voice' and len(audio) == 1


@pytest.mark.parametrize('unavailable,basis', [('wechat', 'WECHAT_TEXT'),
    ('transport', 'TRANSPORT_UNAVAILABLE'), ('provider', 'PROVIDER_UNAVAILABLE')])
def test_default_speech_still_requires_actual_channel_transport_and_provider(monkeypatch, tmp_path, unavailable, basis):
    server, row, _, _, audio = server_fixture(monkeypatch, tmp_path, ordinary_chat_result(delivery='text'))
    channel = 'wechat' if unavailable == 'wechat' else 'qq'
    if unavailable == 'transport': row['voice_available'] = False
    if unavailable == 'provider': server._voice_reply_configured = lambda _: False
    row['channel'] = channel
    asyncio.run(backend.generate(server, PersonalMessage(channel, 'b', 'u', '1', '你好'), row))
    assert row['requested_format'] == 'text' and audio == []
    assert row['voice_ready'] is False and row['delivery_basis'] == basis


def test_twelve_ordinary_qq_turns_deliver_audio_without_text_opt_in_or_replay(monkeypatch, tmp_path):
    server, _, _, _, prepared = server_fixture(monkeypatch, tmp_path, ordinary_chat_result(delivery='text'))
    # The fixture answers every turn with the same canned text; the repeat guard
    # (regenerate a reply identical to a recent one) is not what this test covers.
    from runtime.personal_chat import decision as decision_module
    monkeypatch.setattr(decision_module, 'repeats_recent', lambda *a, **k: False)
    rows, generated, sent = [], [], []
    server.store.personal_chats = rows
    async def generate(event, row):
        generated.append(event.exchange_id)
        return await backend.generate(server, event, row)
    async def commit(row): pass
    async def text_send(text): raise AssertionError('ordinary QQ turn should deliver speech')
    async def audio_send(path):
        sent.append(path)
        return 'confirmed-' + str(len(sent))
    text_send.audio = audio_send
    server._persist_store_state = lambda: None
    async def scenario():
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('b', 'u')})
        for index in range(12):
            event = PersonalMessage('qq', 'b', 'u', str(index), '今天怎么样？')
            await service.handle(event, text_send)
            await service.handle(event, text_send)  # A platform replay must not synthesize/send twice.
        assert len(generated) == len(prepared) == len(sent) == 12
        assert all(row['delivery_status'] == 'DELIVERED' and row['delivered_format'] == 'audio' for row in rows)
        assert all(row['delivery_basis'] == 'QQ_DEFAULT_VOICE' for row in rows)
        await asyncio.gather(*service.consumer_tasks.values())
    asyncio.run(scenario())


def test_optional_default_voice_render_failure_still_delivers_reply_text(monkeypatch, tmp_path):
    server, _, _, _, _ = server_fixture(monkeypatch, tmp_path, ordinary_chat_result(delivery='text'))
    rows, sent = [], []
    server.store.personal_chats = rows
    server._persist_store_state = lambda: None
    async def unavailable(*a, **kwargs): raise RuntimeError('synthetic TTS failure')
    monkeypatch.setattr(backend, 'prepare_chat_audio', unavailable)
    async def generate(event, row): return await backend.generate(server, event, row)
    async def commit(row): pass
    async def text_send(text):
        sent.append(text)
        return 'confirmed-text'
    async def audio_send(path): raise AssertionError('missing audio must not be sent')
    text_send.audio = audio_send
    async def scenario():
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('b', 'u')})
        event = PersonalMessage('qq', 'b', 'u', '1', '今天怎么样？')
        await service.handle(event, text_send)
        assert sent == ['今天练得挺顺，刚歇下来']  # Existing QQ text formatting removes sentence-final dots.
        assert rows[0]['delivery_status'] == 'DELIVERED' and rows[0]['delivered_format'] == 'text'
        assert rows[0]['voice_fallback'] == 'PERSONAL_CHAT_TTS_UNAVAILABLE'
        assert rows[0]['delivery_basis'] == 'VOICE_RENDER_FAILED'
        await asyncio.gather(*service.consumer_tasks.values())
    asyncio.run(scenario())


@pytest.mark.parametrize('timing', ['wait_user', 'defer', 'no_reply'])
def test_backend_silent_decision_does_not_decode_empty_writer_or_render(monkeypatch, tmp_path, timing):
    server, row, _, saved, audio = server_fixture(monkeypatch, tmp_path, pipeline_result(timing=timing, text=''))
    assert asyncio.run(backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', 'Hello'), row)) is None
    assert row['companion_timing'] == timing and row['companion_decision'] == RECORD
    assert 'requested_format' not in row and audio == []
    assert saved[-1]['companion_timing'] == timing


@pytest.mark.parametrize('supersession', ['revision', 'new_receipt'])
def test_backend_obsolete_pipeline_result_cannot_replace_frozen_decision(monkeypatch, tmp_path, supersession):
    async def change():
        if supersession == 'revision':
            row['input_revision'] = 1
        else:
            current[0] = False
    server, row, _, _, audio = server_fixture(monkeypatch, tmp_path, pipeline_result(), run_hook=change)
    current = [True]
    async def scenario():
        token = TURN_IS_CURRENT.set(lambda: current[0])
        try:
            await backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', 'Hello'), row)
        finally:
            TURN_IS_CURRENT.reset(token)
    asyncio.run(scenario())
    assert 'companion_decision' not in row and audio == []


def test_backend_unenabled_reply_keeps_existing_media_behavior(monkeypatch, tmp_path):
    server, row, _, _, audio = server_fixture(monkeypatch, tmp_path, pipeline_result(record=False))
    assert asyncio.run(backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', 'Hello'), row)) == 'Hello'
    assert row['requested_format'] == 'voice' and audio == ['Hello']
    assert 'companion_decision' not in row


def test_pipeline_without_decision_clears_failed_previous_record(monkeypatch, tmp_path):
    server, row, _, _, audio = server_fixture(monkeypatch, tmp_path, pipeline_result(record=False))
    row.update(companion_decision=deepcopy(RECORD), companion_timing='now', companion_delivery='text')
    assert asyncio.run(backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', 'Hello'), row)) == 'Hello'
    assert not any(key in row for key in ('companion_decision', 'companion_timing', 'companion_delivery'))
    assert audio == ['Hello']


def test_decision_is_saved_before_writer_failure_and_reused_on_retry(monkeypatch, tmp_path):
    from runtime.personal_chat.presentation import CURRENT
    server, _, _, _, _ = server_fixture(monkeypatch, tmp_path, pipeline_result())
    saved = []
    server._persist_store_state = lambda: saved.append(deepcopy(server.store.personal_chats))
    calls, sent = [], []
    async def generate(event, current):
        # Exercise the actual backend on the service-owned persistent row.
        server.store.personal_chats = [current]
        return await backend.generate(server, event, current)
    async def run(request, context):
        calls.append(CURRENT.get()['companion_decision'])
        if len(calls) == 1:
            await CURRENT.get()['save_companion_decision'](RECORD)
            assert saved[-1][0]['companion_decision'] == RECORD
            raise RuntimeError('LLM_TIMEOUT')
        assert calls[-1] == RECORD
        return pipeline_result()
    server.reply_pipeline.run = run
    async def send(text): sent.append(text)
    async def commit(current): pass
    async def scenario():
        rows = []
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('b', 'u')})
        event = PersonalMessage('qq', 'b', 'u', '1', 'Hello')
        with pytest.raises(RuntimeError, match='LLM_TIMEOUT'):
            await service.handle(event, send)
        assert rows[0]['companion_decision'] == RECORD
        await service.handle(event, send)
        assert calls == [None, RECORD] and sent == ['Hello']
    asyncio.run(scenario())


@pytest.mark.parametrize('supersession', ['revision', 'new_receipt'])
def test_pre_writer_save_rejects_obsolete_turn(monkeypatch, tmp_path, supersession):
    from runtime.personal_chat.presentation import CURRENT
    current = [True]
    async def stale_callback():
        if supersession == 'revision': row['input_revision'] = 1
        else: current[0] = False
        await CURRENT.get()['save_companion_decision'](RECORD)
    server, row, _, _, _ = server_fixture(monkeypatch, tmp_path, pipeline_result(), run_hook=stale_callback)
    async def scenario():
        token = TURN_IS_CURRENT.set(lambda: current[0])
        try:
            with pytest.raises(RuntimeError, match='JEV_INPUT_SUPERSEDED'):
                await backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', 'Hello'), row)
        finally:
            TURN_IS_CURRENT.reset(token)
    asyncio.run(scenario())
    assert 'companion_decision' not in row


def test_jev_voice_render_failure_does_not_fall_back_to_text(monkeypatch, tmp_path):
    server, row, _, _, _ = server_fixture(monkeypatch, tmp_path, pipeline_result(delivery='audio_speech'))
    async def unavailable(*a, **kwargs): raise RuntimeError('synthetic TTS failure')
    monkeypatch.setattr(backend, 'prepare_chat_audio', unavailable)
    async def scenario():
        with pytest.raises(RuntimeError, match='JEV_PLAN_UNSUPPORTED'):
            await backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', 'Hello'), row)
    asyncio.run(scenario())
    assert 'prepared_audio' not in row and row['companion_decision'] == RECORD


def test_failed_writer_result_retains_pre_writer_decision(monkeypatch, tmp_path):
    from runtime.personal_chat.presentation import CURRENT
    async def save():
        await CURRENT.get()['save_companion_decision'](RECORD)
    server, row, _, _, _ = server_fixture(monkeypatch, tmp_path,
        pipeline_result(record=False, error='LLM_TIMEOUT'), run_hook=save)
    async def scenario():
        with pytest.raises(RuntimeError, match='LLM_TIMEOUT'):
            await backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', 'Hello'), row)
    asyncio.run(scenario())
    assert row['companion_decision'] == RECORD


@pytest.mark.parametrize('enabled', [False, True])
def test_single_jev_plan_does_not_append_mailbox_notice_or_sticker(monkeypatch, tmp_path, enabled):
    import runtime.personal_chat.stickers
    from runtime.personal_chat.mailbox_notice import NOTICE_TEXT
    result = pipeline_result(record=enabled, text=envelope(text='Hello', sticker='linli-01'))
    server, row, _, _, _ = server_fixture(monkeypatch, tmp_path, result)
    monkeypatch.setattr(runtime.personal_chat.stickers, 'choices', lambda *a, **k: {'linli-01': 'synthetic'})
    server.store.letters = [dict(letter_id='unread', origin='proactive', letter_status='COMPLETED',
        reply_mode='text_letter', reply_text='Earlier letter', is_read=0, published_at=1)]
    text = asyncio.run(backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', 'Hello'), row))
    if enabled:
        assert text == 'Hello'
        assert 'mailbox_notice_letter_id' not in row and 'sticker_id' not in row
    else:
        assert NOTICE_TEXT in text
        assert row['mailbox_notice_letter_id'] == 'unread' and row['sticker_id'] == 'linli-01'


@pytest.mark.parametrize('timing', ['wait_user', 'defer', 'no_reply'])
def test_service_silent_decision_is_durable_without_delivery_or_consumers(timing):
    async def scenario():
        rows, generated = [], []
        async def generate(event, row):
            generated.append(event.text)
            row.update(companion_decision=deepcopy(RECORD), companion_timing=timing)
            return None
        async def forbidden(*a): raise AssertionError('silent turn must not send or commit')
        service = PersonalChatService(rows, lambda: None, generate, forbidden,
            {'qq': ('b', 'u')}, photo=forbidden)
        event = PersonalMessage('qq', 'b', 'u', '1', 'Hello')
        await service.handle(event, forbidden)
        assert rows[0]['delivery_status'] == rows[0]['letter_status'] == 'SKIPPED'
        assert not service.photo_tasks and not service.consumer_tasks
        restarted = PersonalChatService(json.loads(json.dumps(rows)), lambda: None, generate,
            forbidden, service.bindings, photo=forbidden)
        await restarted.handle(event, forbidden)
        assert generated == ['Hello']
    asyncio.run(scenario())


def test_silent_old_decision_merges_new_intake_before_skipping():
    async def scenario():
        rows, seen, sent = [], [], []
        first, newer = (PersonalMessage('qq', 'b', 'u', key, text)
                        for key, text in [('1', 'wait'), ('2', 'please answer now')])
        async def generate(event, row):
            seen.append(event.text)
            if len(seen) == 1:
                row.update(companion_decision=deepcopy(RECORD), companion_timing='wait_user', companion_delivery=None)
                await service.ingest(newer)
                return None
            assert not any(k in row for k in ('companion_decision', 'companion_timing', 'companion_delivery'))
            return 'answer'
        async def send(text): sent.append(text)
        async def commit(row): pass
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('b', 'u')})
        await service.handle(first, send)
        assert len(seen) == 2 and 'please answer now' in seen[-1]
        assert sent == ['answer'] and rows[0]['input_revision'] == 1
    asyncio.run(scenario())


def test_obsolete_pre_writer_save_failure_merges_intake_instead_of_failing(monkeypatch, tmp_path):
    from runtime.personal_chat.presentation import CURRENT
    server, _, _, _, _ = server_fixture(monkeypatch, tmp_path, pipeline_result())
    async def scenario():
        rows, calls, sent = [], [], []
        async def run(request, context):
            calls.append(CURRENT.get()['raw_user_text'])
            if len(calls) == 1:
                await service.ingest(PersonalMessage('qq', 'b', 'u', '2', 'please answer now'))
                with pytest.raises(RuntimeError, match='JEV_INPUT_SUPERSEDED'):
                    await CURRENT.get()['save_companion_decision'](RECORD)
                return pipeline_result(record=False, error='JEV_DECISION_NOT_SAVED', text='')
            return pipeline_result()
        server.reply_pipeline.run = run
        server.store.personal_chats = rows
        async def generate(event, row): return await backend.generate(server, event, row)
        async def send(text): sent.append(text)
        async def commit(row): pass
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('b', 'u')})
        await service.handle(PersonalMessage('qq', 'b', 'u', '1', 'wait'), send)
        assert len(calls) == 2 and 'please answer now' in calls[-1]
        assert sent == ['Hello'] and rows[0]['delivery_status'] == 'DELIVERED'
        assert rows[0]['input_revision'] == 1
    asyncio.run(scenario())


def test_unexplained_none_remains_invalid():
    async def scenario():
        rows = []
        async def generate(event, row): return None
        async def forbidden(*a): raise AssertionError('no send')
        service = PersonalChatService(rows, lambda: None, generate, forbidden, {'qq': ('b', 'u')})
        with pytest.raises(ValueError, match='PERSONAL_CHAT_REPLY_INVALID'):
            await service.handle(PersonalMessage('qq', 'b', 'u', '1', 'Hello'), forbidden)
    asyncio.run(scenario())


def test_new_draft_removes_all_jev_fields():
    row = {'content': 'new', 'companion_decision': deepcopy(RECORD),
           'companion_timing': 'now', 'companion_delivery': 'text'}
    _clear_draft(row)
    assert row == {'content': 'new'}


@pytest.mark.parametrize('outcome', ['disconnected', 'unknown', 'delivered'])
def test_recovery_reuses_decision_without_replanning_or_extra_photos(outcome):
    async def scenario():
        rows, calls = [], []
        async def generate(event, row):
            calls.append('generate')
            row.update(companion_decision=deepcopy(RECORD), companion_timing='now', companion_delivery='text')
            return 'answer'
        async def commit(row): calls.append('commit')
        async def photo(*a): calls.append('photo')
        class Send:
            available = outcome != 'disconnected'
            def is_available(self): return self.available
            async def __call__(self, text):
                calls.append('send')
                if outcome == 'unknown': raise TimeoutError()
            async def image(self, *a): calls.append('image')
        send = Send()
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('b', 'u')}, photo=photo)
        event = PersonalMessage('qq', 'b', 'u', '1', 'Hello')
        if outcome != 'delivered':
            with pytest.raises((RuntimeError, TimeoutError)):
                await service.handle(event, send)
        else:
            await service.handle(event, send)
        if service.consumer_tasks: await asyncio.gather(*service.consumer_tasks.values())
        frozen_rows = json.loads(json.dumps(rows))
        restarted = PersonalChatService(frozen_rows, lambda: None, generate, commit, service.bindings, photo=photo)
        send.available = True
        if outcome == 'unknown':
            with pytest.raises(RuntimeError, match='REQUIRES_ATTENTION'):
                await restarted.handle(event, send)
        else:
            await restarted.handle(event, send)
        if restarted.consumer_tasks: await asyncio.gather(*restarted.consumer_tasks.values())
        assert calls.count('generate') == 1 and calls.count('send') == 1
        assert 'photo' not in calls and 'image' not in calls
        assert frozen_rows[0]['companion_decision'] == RECORD
    asyncio.run(scenario())


def test_jev_errors_remain_bounded_diagnostics(monkeypatch, tmp_path):
    server, row, _, _, _ = server_fixture(monkeypatch, tmp_path,
        pipeline_result(error='JEV_PLAN_UNSUPPORTED'))
    async def scenario():
        with pytest.raises(RuntimeError, match='^JEV_PLAN_UNSUPPORTED$'):
            await backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', 'Hello'), row)
    asyncio.run(scenario())
    assert backend._failure_code(RuntimeError('JEV_PLAN_UNSUPPORTED')) == 'JEV_PLAN_UNSUPPORTED'
    assert backend._generation_failure_code('JEV_PRIVATE_USER_TEXT') == 'PERSONAL_CHAT_GENERATION_FAILED'
    assert backend._failure_code(RuntimeError('JEV_PRIVATE_USER_TEXT')) == 'PERSONAL_CHAT_UNAVAILABLE'


@pytest.mark.parametrize('code', ['JEV_PLAN_UNSUPPORTED', 'JEV_RESPONSE_INVALID', 'JEV_HTTP_503', 'JEV_DECISION_NOT_SAVED'])
def test_service_jev_error_survives_status_diagnostics(code):
    async def scenario():
        rows = []
        async def generate(event, row): raise RuntimeError(code)
        async def forbidden(*a): raise AssertionError('no send')
        service = PersonalChatService(rows, lambda: None, generate, forbidden, {'qq': ('b', 'u')})
        with pytest.raises(RuntimeError, match=code):
            await service.handle(PersonalMessage('qq', 'b', 'u', '1', 'Hello'), forbidden)
        assert rows[0]['error_code'] == code
        assert backend.reply_errors(SimpleNamespace(store=SimpleNamespace(personal_chats=rows)),
            {'status': {'qq': 'connected'}}) == {'qq': code}
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['missing_audio', 'upload'])
def test_jev_audio_delivery_failure_cannot_silently_send_text(failure):
    async def scenario():
        rows, sent = [], []
        async def generate(event, row):
            row.update(companion_decision=deepcopy(RECORD), companion_timing='now', companion_delivery='audio_speech')
            if failure == 'upload': row['prepared_audio'] = 'synthetic.wav'
            return 'spoken words'
        class Send:
            async def __call__(self, text): sent.append(text)
            async def audio(self, path): sent.append(path)
            async def prepare_audio(self, path): raise RuntimeError('upload failed')
        async def commit(row): raise AssertionError('no commit')
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('b', 'u')})
        with pytest.raises(RuntimeError, match='JEV_PLAN_UNSUPPORTED'):
            await service.handle(PersonalMessage('qq', 'b', 'u', '1', 'Hello'), Send())
        assert sent == [] and rows[0]['delivery_status'] == 'FAILED'
        assert rows[0]['error_code'] == 'JEV_PLAN_UNSUPPORTED'
    asyncio.run(scenario())
