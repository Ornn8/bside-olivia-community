from runtime.reply.jev_limits import JEV_MAX_INPUT_BYTES
import asyncio
import json
from datetime import datetime, timezone, timedelta

from runtime.private_world.current_affect import CurrentAffect, context
from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.jev_emotion import appraise
from tests.private_world.test_jev_emotion import Decisions


def test_failure_diagnostic_is_persistent_and_never_contains_exception_prose(tmp_path):
    from runtime.private_world.character_emotion_runtime import CharacterEmotionRuntime
    now = datetime(2026, 9, 28, 10, tzinfo=timezone.utc)
    store = DailyLifeStore(tmp_path / 'diagnostic.db')
    runtime = CharacterEmotionRuntime(store, lambda: object(), lambda: '')
    runtime._record_failure(ValueError('JEV_EMOTION_CONCERN_CONFLICT'), 'jev_projection', now)
    assert runtime.view(now)['last_evaluation_error']['code'] == 'JEV_EMOTION_CONCERN_CONFLICT'
    runtime._record_failure(ValueError('secret original user text'), 'commit', now)
    reopened = CharacterEmotionRuntime(store, lambda: object(), lambda: '')
    diagnostic = reopened.view(now)['last_evaluation_error']
    assert diagnostic['code'] == 'EMOTION_EVALUATION_FAILED'
    assert 'secret' not in json.dumps(diagnostic)


def test_eight_sources_with_concerns_and_current_affect_fit_one_packet(tmp_path):
    now = datetime(2026, 9, 28, 10, 8, tzinfo=timezone.utc)
    store = DailyLifeStore(tmp_path / 'world.db')
    persona = '音乐专业学生，关心他人也保留自主决定权，疲倦时需要休息。'
    sources = [dict(source_id=f'budget-{i}', source_kind='received_input', source_order=i,
                    occurred_at=f'2026-09-28T10:0{i}:00+00:00',
                    text=f'第{i}次练习遇到困难，我想先休息。请不要继续催促，让我整理一下。') for i in range(8)]
    concerns = [dict(id=f'old-{i}', summary=f'第{i}段练习还不顺畅。', status='open',
                     occurred_at='2026-09-28T09:00:00+00:00') for i in range(4)]
    contexts = {s['source_id']: dict(as_of=s['occurred_at'], prior_appraisals=[], concerns=concerns,
                    rhythm=dict(phase='day', fatigue='tired'), recent_dialogue=[],
                    relationship=dict(active_boundaries=[], stage_at_source_time='unknown')) for s in sources}
    plan = CurrentAffect(store).prepare(context(store.snapshot(now), {}, persona), now=now)
    for i in range(8):
        plan['questions']['reason']['criteria'][f'batch_source_{i}'] = {
            'batch_source': f's{i}', 'meaning': '本批来源的原文；用户表达不是外部世界事实'}
    port = Decisions()
    packet = dict(persona=persona, assessment=dict(sources=sources, source_contexts=contexts),
                  current_affect={k: plan[k] for k in ('state', 'questions')})
    result = asyncio.run(appraise(port, packet))
    assert len(port.calls) == 1 and len(result['appraisals']) == 8
    state, questions, purpose = port.calls[0]
    wire = json.dumps(dict(state=state, questions=questions, purpose=purpose),
                      ensure_ascii=False, separators=(',', ':')).encode()
    print('eight-source-emotion-wire-bytes', len(wire))
    assert len(wire) <= JEV_MAX_INPUT_BYTES
    assert [s['source']['text'] for s in state['sources'].values()] == [s['text'] for s in sources]
    assert all(c in state['shared_context'].values() for c in concerns)


def test_bundled_affect_reason_references_only_visible_same_packet_evidence(tmp_path):
    from runtime.private_world.jev_emotion import prepare
    now = datetime(2026, 9, 28, 10, tzinfo=timezone.utc)
    store = DailyLifeStore(tmp_path/'paths.db')
    world = store.snapshot(now)
    world['world'] = {'recent_episodes': [{'result': {'detail': '完成练习。'},
        'process': [{'obstacle': '遇到困难', 'response': '慢练', 'outcome': '解决'}]}]}
    emotion = {'reactions': [{'quote': '我很高兴。'}], 'concerns': [{'summary': '还有一件事。'}]}
    affect = CurrentAffect(store).prepare(context(world, emotion, ''), now=now)
    assert any(isinstance(v, dict) and 'state_path' in v for v in affect['questions']['reason']['criteria'].values())
    source = dict(source_id='new', text='今天练习完成了。', occurred_at=now.isoformat())
    affect['questions']['reason']['criteria']['batch_source_0'] = {'batch_source':'s0'}
    plan = prepare(dict(persona='', assessment=dict(sources=[source], source_contexts={
        'new': dict(concerns=[], prior_appraisals=[])}), current_affect={k:affect[k] for k in ('state','questions')}))
    state, questions = plan[:2]
    for value in questions['affect_reason']['criteria'].values():
        if isinstance(value, dict) and 'state_path' in value:
            target = state['current_affect']
            for part in value['state_path']:
                target = target[part]
        if isinstance(value, dict) and 'batch_source' in value:
            assert value['batch_source'] in state['sources']
    assert set(questions['affect_reason']['criteria']) == {'unknown','body','batch_source_0'}


def test_large_batch_sends_one_full_evidence_prefix_and_keeps_rest_pending(tmp_path, monkeypatch):
    from runtime.private_world.character_emotion_runtime import CharacterEmotionRuntime
    from runtime.reply import jev_questions
    from tests.private_world.test_character_emotion_runtime import receipt, NOW
    records = [receipt(f'long-{i}', f'第{i}次练习遇到困难，需要休息。' * 120,
                       NOW - timedelta(minutes=8-i)) for i in range(8)]
    class DelayedDecisions(Decisions):
        async def ask(self, state, questions, *, purpose):
            answers = await super().ask(state, questions, purpose=purpose)
            await asyncio.sleep(0.02)
            return answers
    port = DelayedDecisions()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    store = DailyLifeStore(tmp_path / 'large.db')
    runtime = CharacterEmotionRuntime(store, lambda: object(), lambda: '音乐专业学生')
    async def run():
        return await asyncio.gather(runtime.evaluate_received(records, now=NOW),
                                    runtime.evaluate_received(records, now=NOW))
    views = asyncio.run(run())
    assert all(view['pending_current_input'] for view in views)
    assert runtime.error_code is None
    assert len(port.calls) == 1
    state, questions, purpose = port.calls[0]
    chosen = list(state['sources'].values())
    assert 0 < len(chosen) < len(records)
    assert [s['source']['text'] for s in chosen] == [r.user_message for r in records[:len(chosen)]]
    pending = runtime.store.pending_source_ids(before=NOW, limit=32)
    assert len(pending) == 8-len(chosen)
    assert len(json.dumps(dict(state=state, questions=questions, purpose=purpose),
                          ensure_ascii=False, separators=(',', ':')).encode()) <= JEV_MAX_INPUT_BYTES
    print('oversize-prefix-sources', len(chosen), 'pending', len(pending))
    reopened = CharacterEmotionRuntime(DailyLifeStore(store.path), lambda: object(), lambda: '音乐专业学生')
    assert reopened.store.pending_source_ids(before=NOW, limit=32) == pending
    assert reopened.view(NOW)['pending_current_input']
    assert reopened.view(NOW)['current_affect']['pending_sources']


def test_irrelevant_history_never_enters_emotion_packet_and_concern_anchor_remains():
    from runtime.private_world.jev_emotion import prepare
    source = dict(source_id='new', occurred_at='2026-09-28T10:00:00+00:00',
                  source_kind='received_input', source_order=11, text='对不起，不催你了。')
    prior = [dict(source_id=f'old-{i}', occurred_at=f'2026-09-28T09:{i:02d}:00+00:00',
                  quote=f'第{i}条原始证据。', reaction='frustrated') for i in range(10)]
    concern = dict(id='emotion:old-0', summary='仍在意被催促。', status='open', occurred_at=prior[0]['occurred_at'],
                   source_ids=['old-0', 'old-4'])
    context = dict(prior_appraisals=prior, concerns=[concern], rhythm={'phase':'day'},
                   recent_dialogue=['UNRELATED_DIALOGUE' * 1000], character_development={'raw':'UNRELATED_GROWTH' * 1000})
    packet = dict(persona='学生', assessment=dict(sources=[source], source_contexts={'new':context}))
    state, questions, *_ = prepare(packet)
    wire = json.dumps(dict(state=state, questions=questions), ensure_ascii=False)
    assert 'UNRELATED_' not in wire
    source_view = state['sources']['s0']
    shown = [state['shared_context'][ref['shared_context']] for ref in source_view['prior_appraisals'].values()]
    assert {item['source_id'] for item in shown} == {'old-0', 'old-9'}
    assert all(item['quote'] in {p['quote'] for p in prior} for item in shown)
    assert len(questions['s0_revision']['criteria']) == 3  # none + only visible anchors.
    assert source_view['source']['text'] == source['text']
    assert source_view['history_coverage']['omitted_prior_appraisals'] == 8
    assert len(wire.encode()) < 12000
