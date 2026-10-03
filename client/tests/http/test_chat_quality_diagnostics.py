import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
import io
import json
from types import SimpleNamespace
import zipfile

import pytest
from reply_orchestrator import ReplyState
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.personal_chat import backend
from runtime.personal_chat.events import PersonalMessage
from runtime.diagnostics.support_bundle import build_diagnostic_bundle, project_chat_task
from tests.http.test_diagnostic_support_bundle import _source


@pytest.mark.parametrize('code', ['CURRENT_TURN_INTERPRETATION_FAILED', 'REVIEW_FAILED',
    'REPLY_QUALITY_BLOCKED', 'REWRITE_FAILED', 'REVIEWER_RESPONSE_INVALID', 'PERSONAL_CHAT_DECISION_INVALID'])
def test_semantic_failure_mapping_preserves_finite_layer(code):
    expected = code if code.startswith('PERSONAL_CHAT_') else 'PERSONAL_CHAT_' + code
    assert backend._generation_failure_code(code) == expected
    assert backend._generation_failure_code(expected) == expected
    assert backend._generation_failure_code('private user text sk-secret') == 'PERSONAL_CHAT_GENERATION_FAILED'
    assert backend._generation_failure_code('PERSONAL_CHAT_PRIVATE_USER_TEXT') == 'PERSONAL_CHAT_GENERATION_FAILED'


@pytest.mark.parametrize('failed', [False, True, 'persist_failure'])
def test_pipeline_quality_persists_before_failure_and_survives_bundle(monkeypatch, failed):
    import persona_loader
    import runtime.image_reply
    import runtime.image_understanding
    import runtime.personal_chat.stickers
    from tests.http.test_personal_chat_decision import envelope
    monkeypatch.setattr(persona_loader, 'load_persona', lambda _: SimpleNamespace(snapshot=SimpleNamespace(status='READY')))
    monkeypatch.setattr(runtime.image_reply, 'photo_reply_context', lambda ctx, *a, **k: ctx)
    monkeypatch.setattr(runtime.personal_chat.stickers, 'choices', lambda *a, **k: {})
    async def understand(*a): pass
    monkeypatch.setattr(runtime.image_understanding, 'understand_incoming', understand)
    row = {'channel': 'qq', 'life_received_at': datetime.now(timezone.utc).isoformat()}
    saved = []
    result = SimpleNamespace(state=ReplyState.FAILED if failed is True else ReplyState.COMPLETED,
        error_code='REPLY_QUALITY_BLOCKED' if failed is True else None,
        text=envelope(text='', skip=True, silence={'kind': 'no_reply', 'evidence': 'private incoming'}),
        silence_authorized=True,
        quality_status='blocked' if failed is True else 'accepted', reviewer_calls=2, rewrite_calls=1,
        decision_rejection_reason='FIELDS' if failed is True else None,
        violation_codes=('MEMORY_FABRICATION', 'private violation text'))
    async def run(*a): return result
    server = SimpleNamespace(letters_adapter=SimpleNamespace(config=SimpleNamespace(persona_v2_enabled=True,
        max_input_chars=50000), persona_v2_path='synthetic', build_reply_context=lambda *a, **k: ReplyContext.create(ReplyMode.FUTURE_IM, trusted_time=TrustedTime(datetime.now(timezone.utc)), future_im_enabled=True)),
        _llm_runtime_ready=lambda _: True, daily_life_runtime=object(), _official_history_private_world_available=lambda: True,
        MEMORY_READY_REPLY_TIMEOUT_SECONDS=1, _conversation_memory_ready_for_reply=lambda: True,
        video_reply_settings_store=SimpleNamespace(image_snapshot=lambda: {'enabled': False}),
        _persist_store_state=lambda: saved.append(dict(row)),
        _CURRENT_LETTER_MEMORY_SOURCE=ContextVar('quality_source'), _CURRENT_LETTER_RECEIPT=ContextVar('quality_receipt'),
        store=SimpleNamespace(personal_chats=[]), _voice_reply_configured=lambda _: False,
        supports_scoped_reasoning=lambda _: False, _reply_pipeline_timeout_seconds=lambda _: 1,
        reply_pipeline=SimpleNamespace(run=run))
    async def scenario():
        if failed == 'persist_failure':
            async def shadow():
                await asyncio.Future()
            async def with_shadow(*a):
                result.semantic_shadow_task = asyncio.create_task(shadow())
                return result
            def persist():
                if 'quality_status' in row:
                    raise OSError('synthetic save failure')
            server.reply_pipeline.run = with_shadow
            server._persist_store_state = persist
            try:
                with pytest.raises(OSError, match='synthetic save failure'):
                    await backend.generate(server, PersonalMessage('qq', 'a', 'u', '1', 'private incoming'), row)
                assert result.semantic_shadow_task in getattr(server, '_semantic_shadow_tasks', set())
                await backend._stop_semantic_shadow(server)
                assert result.semantic_shadow_task.cancelled()
                assert not server._semantic_shadow_tasks
            finally:
                result.semantic_shadow_task.cancel()
                await asyncio.gather(result.semantic_shadow_task, return_exceptions=True)
            return
        if failed:
            with pytest.raises(RuntimeError, match='PERSONAL_CHAT_REPLY_QUALITY_BLOCKED'):
                await backend.generate(server, PersonalMessage('qq', 'a', 'u', '1', 'private incoming'), row)
        else:
            assert await backend.generate(server, PersonalMessage('qq', 'a', 'u', '1', 'private incoming'), row) is None
    asyncio.run(scenario())
    if failed == 'persist_failure':
        return
    assert saved[-1]['quality_status'] == result.quality_status
    assert row['reviewer_calls'] == 2 and row['rewrite_calls'] == 1
    source = _source()
    source['tasks']['items'] = [{**row, 'status': 'failed' if failed else 'completed', 'stage': 'reply_generation', 'elapsed_bucket': 'under_1m'}]
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        item = json.loads(archive.read('tasks.json'))['items'][0]
    assert item['quality_status'] == result.quality_status
    assert item['reviewer_calls'] == 2 and item['rewrite_calls'] == 1
    assert item['quality_violation_codes'] == ['MEMORY_FABRICATION']
    if failed is True:
        assert item['decision_rejection_reason'] == 'FIELDS'
    else:
        assert 'decision_rejection_reason' not in item
    assert 'private' not in json.dumps(item)
    assert project_chat_task(project_chat_task(row)) == project_chat_task(row)


def test_quality_projection_rejects_arbitrary_values_and_boolean_counts():
    assert project_chat_task({'channel': 'qq', 'quality_status': 'private text', 'reviewer_calls': True,
                              'rewrite_calls': 999, 'violation_codes': ['private text'],
                              'decision_rejection_reason': 'private text'}) == {'channel': 'qq'}


def test_stage_and_voice_diagnostics_are_numeric_bounded_and_idempotent():
    source = {'channel': 'qq', 'stage_timing_seconds': {'world': .25, 'emotion': .25,
              'total': .3, 'writer': True, 'quality': float('nan'), 'private input': 'secret'},
              'stage_cache_hits': {'writer': 1, 'reviewer': 2, 'rewriter': True, 'private': 1},
              'stage_actual_calls': {'writer': 1, 'reviewer': 3, 'rewriter': -1},
              'voice_prepare_status': 'timeout', 'voice_prepare_seconds': 60.1,
              'voice_prepare_timeout_seconds': 60, 'delivery_basis': 'VOICE_RENDER_TIMEOUT',
              'stage_cache': {'text': 'private model reply', 'key': 'sk-secret'}}
    result = project_chat_task(source)
    assert result['stage_timing_seconds'] == {'world': .25, 'emotion': .25, 'total': .3}
    assert result['stage_cache_hits'] == {'writer': 1, 'reviewer': 2}
    assert result['stage_actual_calls'] == {'writer': 1, 'reviewer': 3}
    assert result['voice_prepare_timeout_seconds'] == 60
    assert result['voice_prepare_status'] == 'timeout'
    assert result['delivery_basis'] == 'VOICE_RENDER_TIMEOUT'
    assert 'private' not in json.dumps(result) and 'sk-secret' not in json.dumps(result)
    assert project_chat_task(result) == result


@pytest.mark.parametrize('invalid', [True, float('inf'), float('-inf'), float('nan'), -1, 'sk-secret', 86401])
def test_malformed_duration_never_enters_bundle(invalid):
    projected = project_chat_task({'channel': 'qq', 'stage_timing_seconds': {'writer': invalid},
                                  'voice_prepare_seconds': invalid, 'voice_prepare_timeout_seconds': invalid})
    assert projected == {'channel': 'qq'}


def test_letter_quality_diagnostics_survive_bundle_without_body_or_chat_channel():
    row = {'quality_status': 'blocked', 'reviewer_calls': 1, 'rewrite_calls': 1,
           'quality_error_code': 'REWRITE_INPUT_TOO_LARGE',
           'quality_failure_stage': 'rewrite', 'reply_text': 'private draft',
           'exception_text': 'private exception',
           'quality_violation_codes': ['MEMORY_FABRICATION', 'INTERNAL_CONTROL_MARKUP']}
    assert project_chat_task(row) == {key: value for key, value in row.items()
                                     if key not in {'reply_text', 'exception_text'}}
    source = _source()
    source['tasks']['items'] = [{**row, 'status': 'failed', 'stage': 'reply_generation',
                                'elapsed_bucket': 'under_1m'}]
    source['runtime_tail'] = [{**row, 'event': 'letter_failed',
                              'cause_code': 'REWRITE_INPUT_TOO_LARGE'}]
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        item = json.loads(archive.read('tasks.json'))['items'][0]
        event = json.loads(archive.read('runtime-tail.jsonl').splitlines()[0])
        manifest = json.loads(archive.read('manifest.json'))
    from pathlib import Path
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).resolve().parents[2] /
                         'contracts/diagnostic_bundle_manifest.schema.json').read_text(encoding='utf-8'))
    Draft202012Validator(schema).validate(manifest)
    assert 'reply_quality_violation_codes' in manifest['features']
    assert item['quality_error_code'] == 'REWRITE_INPUT_TOO_LARGE'
    assert item['rewrite_calls'] == 1
    assert item['quality_violation_codes'] == row['quality_violation_codes']
    assert 'private' not in json.dumps(item)
    assert event['cause_code'] == 'REWRITE_INPUT_TOO_LARGE'
    assert event['quality_failure_stage'] == 'rewrite'
    assert event['quality_violation_codes'] == row['quality_violation_codes']
    assert 'private' not in json.dumps(event)
    assert project_chat_task({'quality_error_code': 'REWRITE_PRIVATE_TEXT',
                              'quality_failure_stage': 'private text'}) == {}


@pytest.mark.parametrize('counts', [(-1, -1), (3, 2), (1.0, False), ('2', '1')])
def test_quality_counts_are_strict_bounded_integers(counts):
    assert project_chat_task({'channel': 'qq', 'reviewer_calls': counts[0], 'rewrite_calls': counts[1]}) == {'channel': 'qq'}


def test_quality_violation_projection_allowlists_codes_and_is_idempotent():
    raw = {'quality_status': 'blocked', 'quality_violation_codes': [
        'MEMORY_FABRICATION', 'STYLE_DRIFT', 'MEMORY_FABRICATION',
        'USER_SECRET_MARKER', 'private user text', {'code': 'BOUNDARY_BREACH'},
        None, True, ['STYLE_DRIFT'], 'INTERNAL_CONTROL_MARKUP'],
        'candidate': 'private reply', 'review_text': 'private reason'}
    result = project_chat_task(raw)
    assert result == {'quality_status': 'blocked', 'quality_failure_stage': 'review',
                      'quality_violation_codes': ['MEMORY_FABRICATION', 'STYLE_DRIFT', 'INTERNAL_CONTROL_MARKUP']}
    assert project_chat_task(result) == result


@pytest.mark.parametrize('invalid', ['MEMORY_FABRICATION', {'code': 'STYLE_DRIFT'}, None, True])
def test_quality_violation_projection_ignores_malformed_containers(invalid):
    assert project_chat_task({'quality_violation_codes': invalid}) == {}


@pytest.mark.parametrize('state', ['SENDING', 'DELIVERED', 'UNKNOWN'])
def test_generation_failure_notice_is_finite_and_survives_bundle_projection(state):
    raw = {'channel': 'qq', 'delivery_status': 'FAILED', 'generation_failure_notice': state,
           'reply_text': 'private unchecked draft', 'failure_notice_text': 'private text'}
    projected = project_chat_task(raw)
    assert projected == {'channel': 'qq', 'delivery_status': 'FAILED', 'generation_failure_notice': state}
    assert project_chat_task(projected) == projected
    assert project_chat_task({'generation_failure_notice': 'private exception'}) == {}
