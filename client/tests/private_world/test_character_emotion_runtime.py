import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from runtime.memory.received_user_originals import ReceivedOriginal
from runtime.private_world.character_emotion import REACTION_WINDOW
from runtime.private_world.character_emotion_runtime import CharacterEmotionRuntime
from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime


NOW = datetime(2026, 9, 27, 4, tzinfo=timezone.utc)
TEXT = '不用着急，你可以先休息一下。'


def receipt(key='m1', text=TEXT, when=NOW):
    return ReceivedOriginal('received-user:qq:' + key, text, when,
                            ('reply:' + key + ':1',), 'qq', key)


def output(packet, *, reaction='relieved'):
    return {'appraisals': [dict(source_id=s['source_id'], quote=s['text'][:240],
        reported_affect=None, reaction=reaction, goal_or_need='按自己的节奏继续',
        action_tendency='rest', concern=None, revises=None) for s in packet['assessment']['sources']]}


class Gateway:
    def __init__(self, transform=None):
        self.calls = []
        self.transform = transform or output

    async def complete_structured_scoped(self, messages, **kwargs):
        packet = json.loads(messages[-1]['content'])
        self.calls.append((packet, kwargs, messages))
        result = self.transform(packet)
        return SimpleNamespace(text=json.dumps(result, ensure_ascii=False))


def runtime(tmp_path, gateway=None):
    life = DailyLifeStore(tmp_path / 'life.sqlite3')
    gateway = gateway or Gateway()
    return CharacterEmotionRuntime(life, lambda: gateway, lambda: '音乐专业大学生'), gateway, life


def test_independent_concern_summary_from_emotion_result_is_preserved(tmp_path):
    summary = '面试准备尚未完成'
    def summarize(packet):
        value = output(packet, reaction='concerned')
        item = value['appraisals'][0]
        item['concern'] = dict(id='emotion:' + item['source_id'], action='open', summary=summary)
        return value
    emotion, gateway, _ = runtime(tmp_path, Gateway(summarize))
    original = receipt(text='我明天的面试还没准备好，担心到时说不清楚。')
    view = asyncio.run(emotion.evaluate_received([original], now=NOW))
    assert view['concerns'][0]['summary'] == summary
    assert view['reactions'][0]['quote'] == '用户说：' + original.user_message
    assert len(gateway.calls) == 1


def test_world_snapshot_exposes_same_emotion_and_evidence_without_new_judgment(tmp_path):
    emotion, gateway, store = runtime(tmp_path)
    expected = asyncio.run(emotion.evaluate_received([receipt()], now=NOW))
    life = DailyLifeRuntime(store, lambda: gateway, lambda: '音乐专业大学生')
    life._emotion = emotion
    calls = len(gateway.calls)
    shown = life.snapshot(NOW)['emotion']
    assert shown['status'] == 'available'
    assert shown['reactions'] == expected['reactions']
    # A received message is the user's words, and its quote says so.
    assert shown['reactions'][0]['quote'] == '用户说：' + TEXT
    assert shown['concerns'] == expected['concerns']
    assert 'reported_affects' not in shown
    assert len(gateway.calls) == calls
    assert life.snapshot(NOW + REACTION_WINDOW + timedelta(seconds=1))['emotion']['reactions'] == []
    emotion.error_code = 'EMOTION_EVALUATION_UNAVAILABLE'
    assert life.snapshot(NOW)['emotion']['status'] == 'unavailable'


def test_received_original_is_appraised_before_reply_and_only_once(tmp_path):
    emotion, gateway, life = runtime(tmp_path)
    async def run():
        view = await emotion.evaluate_received([receipt()], now=NOW)
        assert view['reactions'][0]['reaction'] == 'relieved'
        assert await emotion.evaluate_received([receipt()], now=NOW) == view
        assert await emotion.evaluate_received([], now=NOW) == view
    asyncio.run(run())
    assert len(gateway.calls) == 1
    packet, options, _ = gateway.calls[0]
    assert packet['assessment']['sources'][0]['source_kind'] == 'received_input'
    schema = options['response_format']['schema']
    item = schema['properties']['appraisals']['items']
    assert item['properties']['source_id']['enum'] == [receipt().source_id]
    assert item['additionalProperties'] is False
    assert packet['persona'] == '音乐专业大学生'
    assert packet['rhythm']
    with life._db() as db:
        assert db.execute('SELECT count(*) FROM life_moments').fetchone()[0] == 0


def test_concurrent_identical_failed_appraisal_is_shared_but_later_call_can_retry(tmp_path, monkeypatch):
    from runtime.reply import jev_questions
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    class Port:
        async def ask(self, *args, **kwargs):
            calls.append(kwargs['purpose'])
            entered.set()
            await release.wait()
            raise ValueError('JEV_UNAVAILABLE')
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: Port())
    emotion, gateway, _ = runtime(tmp_path)
    async def scenario():
        owner = asyncio.create_task(emotion.evaluate_received([receipt()], now=NOW))
        await entered.wait()
        waiters = [asyncio.create_task(emotion.evaluate_received([receipt()], now=NOW)) for _ in range(4)]
        await asyncio.sleep(0)
        release.set()
        views = await asyncio.gather(owner, *waiters)
        assert len(calls) == 1
        assert all(view.get('pending_current_input') for view in views)
        assert not emotion._inflight and not gateway.calls
        # An independent later request can recover; this is not a failure cache.
        await emotion.evaluate_received([receipt()], now=NOW)
        assert len(calls) == 2
    asyncio.run(scenario())


def test_emotion_view_recovers_read_error_without_a_new_model_call(tmp_path, monkeypatch):
    emotion, gateway, store = runtime(tmp_path)
    expected = asyncio.run(emotion.evaluate_received([receipt()], now=NOW))
    read = emotion.store.view
    attempts = []
    def flaky(**kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError('synthetic read failure')
        return read(**kwargs)
    monkeypatch.setattr(emotion.store, 'view', flaky)
    assert emotion.view(NOW)['reactions'] == []
    assert emotion.error_code == 'EMOTION_STATE_UNAVAILABLE'
    assert emotion.view(NOW) == expected
    assert emotion.error_code is None
    life = DailyLifeRuntime(store, lambda: gateway, lambda: '[]')
    life._emotion = emotion
    assert life.snapshot(NOW)['emotion']['status'] == 'available'
    assert len(gateway.calls) == 1


@pytest.mark.parametrize('failure', ['EMOTION_EVALUATION_UNAVAILABLE', 'EMOTION_SOURCE_UNAVAILABLE'])
def test_read_recovery_does_not_clear_other_emotion_failure(tmp_path, monkeypatch, failure):
    emotion, _, _ = runtime(tmp_path)
    emotion.error_code = failure
    read = emotion.store.view
    attempts = []
    def flaky(**kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError('synthetic read failure')
        return read(**kwargs)
    monkeypatch.setattr(emotion.store, 'view', flaky)
    emotion.view(NOW)
    emotion.view(NOW)
    assert emotion.error_code == failure


@pytest.mark.parametrize('scoped_budget', [None, 125.0])
def test_emotion_wait_respects_gateway_scope_budget_or_constructor_fallback(tmp_path, monkeypatch, scoped_budget):
    from llm_gateway import GatewayRequestScope
    emotion, gateway, _ = runtime(tmp_path)
    expected_scope_calls = []
    if scoped_budget is not None:
        def budget(scope, *, default):
            expected_scope_calls.append((scope, default))
            return scoped_budget
        gateway.timeout_seconds_for_scope = budget
    timeouts = []
    actual_wait = asyncio.wait_for
    async def capture_wait(awaitable, timeout):
        timeouts.append(timeout)
        return await actual_wait(awaitable, timeout=1)
    monkeypatch.setattr(asyncio, 'wait_for', capture_wait)
    view = asyncio.run(emotion.evaluate_received([receipt()], now=NOW))
    assert view['reactions'] and not view.get('pending_current_input')
    assert timeouts == [(scoped_budget if scoped_budget is not None else 40) + 1]
    assert expected_scope_calls == ([(GatewayRequestScope.BACKGROUND_REASONING, 40)]
                                    if scoped_budget is not None else [])


def test_raw_strings_and_draft_rows_are_not_receipts(tmp_path):
    emotion, gateway, _ = runtime(tmp_path)
    view = asyncio.run(emotion.evaluate_received([TEXT, {'user_message': TEXT}], now=NOW))
    assert view['reactions'] == [] and gateway.calls == []
    assert emotion.store.pending_source_ids(before=NOW) == []


def test_failure_remains_pending_and_restart_refresh_recovers_it(tmp_path):
    def fail(_):
        raise RuntimeError('private key must not appear in error')
    emotion, gateway, life = runtime(tmp_path, Gateway(fail))
    failed = asyncio.run(emotion.evaluate_received([receipt()], now=NOW))
    assert failed['reactions'] == []
    assert emotion.error_code == 'EMOTION_EVALUATION_UNAVAILABLE'
    assert len(gateway.calls) == 1
    assert emotion.store.pending_source_ids(before=NOW) == [receipt().source_id]
    recovered = CharacterEmotionRuntime(life, Gateway, lambda: '大学生')
    view = asyncio.run(recovered.refresh_world(NOW + timedelta(minutes=1)))
    assert view['reactions'][0]['source_id'] == receipt().source_id


def test_next_receipt_recovers_failed_predecessor_and_clarifies_in_one_model_batch(tmp_path):
    earlier = receipt('first', '这件事让我有点担心', NOW - timedelta(seconds=1))
    later = receipt('second', '刚才那件事已经说清楚了', NOW)
    fail_once = True
    def evaluate(packet):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError('synthetic transient outage')
        assert [s['source_id'] for s in packet['assessment']['sources']] == [earlier.source_id, later.source_id]
        value = output(packet)
        value['appraisals'][0]['concern'] = dict(id='concern:first', action='open', summary='关心那件事')
        value['appraisals'][1]['concern'] = dict(id='concern:first', action='resolve', summary='得到澄清')
        return value
    emotion, gateway, _ = runtime(tmp_path, Gateway(evaluate))
    failed = asyncio.run(emotion.evaluate_received([earlier], now=earlier.occurred_at))
    assert failed['pending_current_input'] is True
    view = asyncio.run(emotion.evaluate_received([later], now=NOW))
    assert len(gateway.calls) == 2  # One failed call, then one joint recovery.
    assert emotion.error_code is None
    assert emotion.store.pending_source_ids(before=NOW) == []
    assert view['concerns'] == [] and not view.get('pending_current_input')


def test_oversized_pending_history_does_not_partly_apply_on_duplicate_current(tmp_path):
    emotion, gateway, _ = runtime(tmp_path)
    items = [receipt(str(i), '消息' + str(i), NOW - timedelta(seconds=33-i)) for i in range(33)]
    for item in items:
        emotion.store.receive(item.source_id, item.user_message, occurred_at=item.occurred_at)
    view = asyncio.run(emotion.evaluate_received([items[0]], now=NOW))
    assert gateway.calls == []
    assert view['pending_current_input'] is True
    assert view['reactions'] == []


@pytest.mark.parametrize('mutation', ['unknown_source', 'invented_quote', 'trust_delta', 'wrong_subject'])
def test_invalid_model_authority_is_rejected_atomically(tmp_path, mutation):
    def bad(packet):
        value = output(packet)
        item = value['appraisals'][0]
        if mutation == 'unknown_source': item['source_id'] = 'somebody-else'
        if mutation == 'invented_quote': item['quote'] = '她已经睡过了'
        if mutation == 'trust_delta': item['trust_delta'] = -30
        if mutation == 'wrong_subject':
            item['reported_affect'] = {'subject': 'character', 'quote': TEXT, 'affect': 'hurt'}
        return value
    emotion, gateway, _ = runtime(tmp_path, Gateway(bad))
    result = asyncio.run(emotion.evaluate_received([receipt()], now=NOW))
    assert result['reactions'] == []
    assert emotion.store.pending_source_ids(before=NOW) == [receipt().source_id]
    assert len(gateway.calls) == 2  # One bounded correction; repeated invalid authority never commits.


def test_reported_user_affect_does_not_become_character_reaction(tmp_path):
    def report(packet):
        value = output(packet, reaction='none')
        item = value['appraisals'][0]
        item.update(quote='', action_tendency='none', goal_or_need=None,
                    reported_affect={'subject': 'user', 'quote': '我很烦', 'affect': 'frustrated'})
        return value
    emotion, _, _ = runtime(tmp_path, Gateway(report))
    view = asyncio.run(emotion.evaluate_received([receipt(text='我很烦，老板又催我。')], now=NOW))
    assert view['reactions'] == []
    assert view['reported_affects'][0]['subject'] == 'user'


def test_world_refresh_uses_only_committed_daily_records_not_future_or_exchange(tmp_path):
    emotion, gateway, life = runtime(tmp_path)
    life.record_exchange('reply:generated', '练完了吗', '练完了', [], occurred_at=NOW)
    for key, when in [('day:published', NOW), ('day:future', NOW + timedelta(hours=1))]:
        life.publish_day(key, {'location': '家里', 'activity': '练琴', 'note': '练习左手衔接'}, [], occurred_at=when)
    asyncio.run(emotion.refresh_world(NOW))
    sources = gateway.calls[0][0]['assessment']['sources']
    assert [s['source_id'] for s in sources] == ['day:published']
    assert sources[0]['source_kind'] == 'published_world'
    asyncio.run(emotion.refresh_world(NOW))
    assert len(gateway.calls) == 1


def test_world_refresh_does_not_build_emotion_history_from_legacy_daily_records(tmp_path):
    emotion, gateway, life = runtime(tmp_path)
    for index in range(17):  # More than two discovery batches from before module enable.
        life.publish_day(f'day:legacy:{index}',
                         {'location': '家里', 'activity': '练琴', 'note': '过去的一次练习'}, [],
                         occurred_at=NOW - timedelta(days=index + 1))
    for _ in range(3):
        asyncio.run(emotion.refresh_world(NOW))
    assert gateway.calls == []
    with life._db() as db:
        assert db.execute('SELECT count(*) FROM character_emotion_sources').fetchone()[0] == 0


def test_world_refresh_discovers_recent_boundary_but_not_expired_or_future_sources(tmp_path):
    emotion, gateway, life = runtime(tmp_path)
    for key, when in [
        ('day:expired', NOW - REACTION_WINDOW - timedelta(microseconds=1)),
        ('day:boundary', NOW - REACTION_WINDOW),
        ('day:recent', NOW),
        ('day:future', NOW + timedelta(microseconds=1)),
    ]:
        life.publish_day(key, {'location': '家里', 'activity': '练琴', 'note': '练习一小段'}, [],
                         occurred_at=when)
    asyncio.run(emotion.refresh_world(NOW))
    sources = gateway.calls[0][0]['assessment']['sources']
    assert [source['source_id'] for source in sources] == ['day:boundary', 'day:recent']
    asyncio.run(emotion.refresh_world(NOW))
    assert len(gateway.calls) == 1


def test_world_refresh_recovers_registered_failure_after_restart_outside_discovery_window(tmp_path):
    class FailOnce(Gateway):
        async def complete_structured_scoped(self, messages, **kwargs):
            if not self.calls:
                self.calls.append(None)
                raise RuntimeError('synthetic temporary appraisal failure')
            return await super().complete_structured_scoped(messages, **kwargs)

    emotion, gateway, life = runtime(tmp_path, FailOnce())
    life.publish_day('day:retry', {'location': '家里', 'activity': '练琴', 'note': '练习一小段'}, [],
                     occurred_at=NOW)
    asyncio.run(emotion.refresh_world(NOW))
    assert emotion.store.pending_source_ids(before=NOW) == ['day:retry']
    later = NOW + timedelta(days=2)
    restarted = CharacterEmotionRuntime(life, lambda: gateway, lambda: '音乐专业大学生')
    asyncio.run(restarted.refresh_world(later))
    assert restarted.error_code is None and len(gateway.calls) == 2
    assert restarted.store.pending_source_ids(before=later) == []
    assert gateway.calls[-1][0]['assessment']['sources'][0]['source_id'] == 'day:retry'


def test_world_refresh_registration_budget_skips_registered_sources_and_discovers_new_publication(tmp_path):
    emotion, gateway, life = runtime(tmp_path)
    for index in range(9):
        life.publish_day(f'day:recent:{index}',
                         {'location': '家里', 'activity': '练琴', 'note': '练习一小段'}, [], occurred_at=NOW)
    asyncio.run(emotion.refresh_world(NOW))
    asyncio.run(emotion.refresh_world(NOW))
    with life._db() as db:
        assert db.execute('SELECT count(*) FROM character_emotion_sources').fetchone()[0] == 9
    later = NOW + timedelta(minutes=1)
    life.publish_day('day:new', {'location': '家里', 'activity': '休息', 'note': '现在休息一会儿'}, [],
                     occurred_at=later)
    asyncio.run(emotion.refresh_world(later))
    assert gateway.calls[-1][0]['assessment']['sources'][0]['source_id'] == 'day:new'
    assert emotion.store.pending_source_ids(before=later) == []


def test_late_source_never_sees_future_appraisal_context(tmp_path):
    emotion, gateway, _ = runtime(tmp_path)
    asyncio.run(emotion.evaluate_received([receipt('new')], now=NOW))
    older = NOW - timedelta(hours=1)
    asyncio.run(emotion.evaluate_received([receipt('old', '还没确定', older)], now=NOW))
    assessment = gateway.calls[-1][0]['assessment']
    assert datetime.fromisoformat(assessment['as_of']) == older
    assert all(p['source_id'] != receipt('new').source_id for p in assessment['prior_appraisals'])


def test_simultaneous_same_receipt_does_not_start_a_second_model_call(tmp_path):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        class Blocking(Gateway):
            async def complete_structured_scoped(self, messages, **kwargs):
                started.set()
                await release.wait()
                return await super().complete_structured_scoped(messages, **kwargs)
        emotion, gateway, _ = runtime(tmp_path, Blocking())
        first = asyncio.create_task(emotion.evaluate_received([receipt()], now=NOW))
        await started.wait()
        second = asyncio.create_task(emotion.evaluate_received([receipt()], now=NOW))
        await asyncio.sleep(0)
        assert not second.done()
        release.set()
        assert (await first)['reactions']
        assert (await second)['reactions']
        assert len(gateway.calls) == 1
    asyncio.run(run())


def test_three_overlapping_receipts_finish_after_predecessor_ownership_changes(tmp_path):
    async def run():
        started_a, release_a = asyncio.Event(), asyncio.Event()
        started_b, release_b = asyncio.Event(), asyncio.Event()
        sources = [receipt(key, '消息' + key, NOW - timedelta(seconds=2-i))
                   for i, key in enumerate(('a', 'b', 'c'))]
        class Handoff(Gateway):
            async def complete_structured_scoped(self, messages, **kwargs):
                packet = json.loads(messages[-1]['content'])
                source_ids = [s['source_id'] for s in packet['assessment']['sources']]
                if sources[0].source_id in source_ids:
                    started_a.set()
                    await release_a.wait()
                elif sources[1].source_id in source_ids:
                    started_b.set()
                    await release_b.wait()
                return await super().complete_structured_scoped(messages, **kwargs)
        emotion, gateway, _ = runtime(tmp_path, Handoff())
        first = asyncio.create_task(emotion.evaluate_received([sources[0]], now=NOW))
        await started_a.wait()
        second = asyncio.create_task(emotion.evaluate_received([sources[1]], now=NOW))
        await asyncio.sleep(0)
        third = asyncio.create_task(emotion.evaluate_received([sources[2]], now=NOW))
        await asyncio.sleep(0)
        assert set(emotion._inflight) == {sources[0].source_id}
        release_a.set()
        await started_b.wait()
        assert not second.done() and not third.done()
        release_b.set()
        await first
        await second
        final = await asyncio.wait_for(third, 1)
        assert sources[2].source_id in {item['source_id'] for item in final['reactions']}
        assert not final.get('pending_current_input')
        assert emotion.store.pending_source_ids(before=NOW) == []
        assert len(gateway.calls) == 3
        assert emotion._inflight == {}
    asyncio.run(run())


@pytest.mark.parametrize('owner_fails', [False, True])
def test_overlapping_batch_waits_for_its_owned_predecessor_before_new_clarification(tmp_path, owner_fails):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        earlier = receipt('first', '还有件事没说清楚', NOW - timedelta(seconds=1))
        later = receipt('second', '刚才那件事说清楚了', NOW)
        class Blocking(Gateway):
            started_count = 0
            async def complete_structured_scoped(self, messages, **kwargs):
                self.started_count += 1
                started.set()
                await release.wait()
                return await super().complete_structured_scoped(messages, **kwargs)
        def evaluate(packet):
            if owner_fails and len(gateway.calls) == 1:
                raise RuntimeError('first evaluation temporarily unavailable')
            value = output(packet)
            for item in value['appraisals']:
                if item['source_id'] == earlier.source_id:
                    item['concern'] = dict(id='concern:first', action='open', summary='关心那件事')
                else:
                    if not owner_fails:
                        context = packet['assessment']['source_contexts'][later.source_id]
                        assert context['concerns'][0]['id'] == 'concern:first'
                    item['concern'] = dict(id='concern:first', action='resolve', summary='得到澄清')
            return value
        emotion, gateway, _ = runtime(tmp_path, Blocking(evaluate))
        owner = asyncio.create_task(emotion.evaluate_received([earlier], now=NOW))
        await started.wait()
        merged = asyncio.create_task(emotion.evaluate_received([later], now=NOW))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert gateway.started_count == 1
        release.set()
        await owner
        view = await merged
        assert gateway.started_count == 2
        assert emotion.error_code is None and view['concerns'] == []
        assert not view.get('pending_current_input')
    asyncio.run(run())


def test_merged_receipts_with_different_times_use_one_bounded_model_batch(tmp_path):
    items = [receipt('m' + str(i), '消息' + str(i), NOW - timedelta(seconds=10-i)) for i in range(10)]
    def evaluate(packet):
        value = output(packet)
        value['appraisals'][0]['concern'] = dict(id='concern:m0', action='open', summary='担心一件事')
        value['appraisals'][-1]['concern'] = dict(id='concern:m0', action='resolve', summary='最后一句已澄清')
        return value
    emotion, gateway, _ = runtime(tmp_path, Gateway(evaluate))
    view = asyncio.run(emotion.evaluate_received(items, now=NOW))
    assert len(gateway.calls) == 1
    assessment = gateway.calls[0][0]['assessment']
    assert len(assessment['sources']) == 10
    for source in assessment['sources']:
        context = assessment['source_contexts'][source['source_id']]
        assert context['as_of'] == source['occurred_at']
    assert emotion.store.pending_source_ids(before=NOW) == []
    assert view['concerns'] == [] and not view.get('pending_current_input')


@pytest.mark.parametrize('too_many', [False, True])
def test_oversized_current_batch_is_not_partly_applied_and_reports_pending(tmp_path, too_many):
    emotion, gateway, _ = runtime(tmp_path)
    items = ([receipt(str(i), '消息' + str(i)) for i in range(33)] if too_many
             else [receipt('large', '一个很长的原文' * 5000)])
    view = asyncio.run(emotion.evaluate_received(items, now=NOW))
    assert gateway.calls == []
    assert view['pending_current_input'] is True and view['reactions'] == []
    assert emotion.store.pending_source_ids(before=NOW)


def test_emotion_persona_uses_runtime_asset_traits_without_changing_world_menu_seeds(tmp_path):
    from pathlib import Path
    from llm_gateway import GatewayConfig
    from persona_loader import load_persona
    from runtime.private_world.daily_life_runtime import life_persona
    path = Path(__file__).resolve().parents[2] / GatewayConfig().persona_v2_file
    baseline = json.loads(life_persona(path))
    enriched = json.loads(life_persona(path, include_emotion_traits=True))
    assert len([item for item in baseline if 'statement' in item]) == 6
    taste = next(item for item in baseline if item['declaration_id'] == 'anchor.everyday_taste')
    assert 'statement' not in taste  # Menu examples must not seed every meal.
    assert {item['key'] for item in taste['development']} == {'sweet', 'spicy', 'light_food'}
    phases = [item for item in baseline if item.get('inclusion') == 'phase']
    assert len(phases) == 2 and all('statement' not in item for item in phases)
    assert all(not item['declaration_id'].startswith('trait.') for item in baseline)
    actual = {d.declaration_id: d for d in load_persona(path).snapshot.declarations}
    extra = [item for item in enriched if item not in baseline]
    assert len(extra) == 6  # Four existing traits, autonomy, and not a reward dispenser.
    assert {'constitution.autonomy', 'character.not_reward_dispenser'} <= {d['declaration_id'] for d in extra}
    for item in extra:
        declaration = actual[item['declaration_id']]
        assert (item['statement'], item['tier'], item['confidence']) == (
            declaration.statement, declaration.tier, declaration.confidence)
    gateway = Gateway()
    daily = DailyLifeRuntime(DailyLifeStore(tmp_path / 'life.db'), lambda: gateway,
                             lambda: life_persona(path),
                             emotion_persona=lambda: life_persona(path, include_emotion_traits=True))
    asyncio.run(daily.emotion.evaluate_received([receipt()], now=NOW))
    assert json.loads(gateway.calls[0][0]['persona']) == enriched
    assert json.loads(daily.persona()) == baseline


def test_future_receipt_waits_until_its_event_time(tmp_path):
    emotion, gateway, _ = runtime(tmp_path)
    future = NOW + timedelta(minutes=1)
    asyncio.run(emotion.evaluate_received([receipt(when=future)], now=NOW))
    assert gateway.calls == [] and emotion.view(NOW)['reactions'] == []
    assert emotion.store.pending_source_ids(before=future) == []
    asyncio.run(emotion.evaluate_received([receipt(when=future)], now=future))
    assert len(gateway.calls) == 1


@pytest.mark.parametrize('cancel_owner', [False, True])
def test_cancelled_duplicate_or_owner_never_cancels_the_other_or_leaves_waiters(tmp_path, cancel_owner):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        class Blocking(Gateway):
            async def complete_structured_scoped(self, messages, **kwargs):
                started.set()
                await release.wait()
                return await super().complete_structured_scoped(messages, **kwargs)
        emotion, gateway, _ = runtime(tmp_path, Blocking())
        owner = asyncio.create_task(emotion.evaluate_received([receipt()], now=NOW))
        await started.wait()
        waiter = asyncio.create_task(emotion.evaluate_received([receipt()], now=NOW))
        await asyncio.sleep(0)
        cancelled = owner if cancel_owner else waiter
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        if cancel_owner:
            release.set()
            assert (await asyncio.wait_for(waiter, 1))['reactions']
            assert emotion.store.pending_source_ids(before=NOW) == []
        else:
            assert not owner.done()
            release.set()
            assert (await owner)['reactions']
        assert emotion._inflight == {}
    asyncio.run(run())


@pytest.mark.parametrize('same_time', [False, True])
@pytest.mark.parametrize('operation', ['resolve', 'withdraw'])
def test_one_batch_can_revise_its_own_earlier_source(tmp_path, same_time, operation):
    # Deliberately reverse lexical IDs: chronology follows durable reception order.
    earlier = receipt('z-first', '刚才的话让我有点担心', NOW - timedelta(seconds=1))
    later = receipt('a-second', '刚才表达错了，其实已经没事了', earlier.occurred_at if same_time else NOW)
    concern_id = 'concern:' + earlier.source_id
    def evaluate(packet):
        sources = packet['assessment']['sources']
        assert [s['source_id'] for s in sources] == [earlier.source_id, later.source_id]
        value = output(packet, reaction='concerned')
        first, second = value['appraisals']
        first['concern'] = dict(id=concern_id, action='open', summary='在意刚才的分歧')
        second.update(reaction='relieved', action_tendency='continue')
        if operation == 'resolve':
            second['concern'] = dict(id=concern_id, action='resolve', summary='当事人澄清了原话')
        else:
            second['revises'] = dict(source_id=earlier.source_id, action='withdraw')
        return value
    emotion, gateway, _ = runtime(tmp_path, Gateway(evaluate))
    view = asyncio.run(emotion.evaluate_received([earlier, later], now=NOW))
    assert len(gateway.calls) == 1
    assert emotion.error_code is None
    assert emotion.store.pending_source_ids(before=NOW) == []
    assert view['concerns'] == []
    if operation == 'withdraw':
        assert [r['source_id'] for r in view['reactions']] == [later.source_id]


@pytest.mark.parametrize('always_stale', [False, True])
def test_dependency_change_gets_at_most_one_fresh_snapshot_retry(tmp_path, monkeypatch, always_stale):
    emotion, gateway, _ = runtime(tmp_path)
    original = emotion.store.commit
    calls = []
    def commit(basis, value):
        calls.append(basis)
        if always_stale or len(calls) == 1:
            raise ValueError('EMOTION_CONTEXT_STALE')
        return original(basis, value)
    monkeypatch.setattr(emotion.store, 'commit', commit)
    view = asyncio.run(emotion.evaluate_received([receipt()], now=NOW))
    assert len(gateway.calls) == len(calls) == 2
    assert bool(view['reactions']) is not always_stale


def test_appraisal_failure_does_not_stop_life_publication(tmp_path):
    from tests.private_world.decisions import life_decision
    class Combined:
        async def complete_structured_scoped(self, messages, **kwargs):
            if kwargs['response_format'].get('name') == 'character_emotion':
                raise RuntimeError('synthetic appraisal unavailable')
            return SimpleNamespace(text=json.dumps(life_decision(messages)))
    life = DailyLifeStore(tmp_path / 'life.sqlite3')
    daily = DailyLifeRuntime(life, Combined, lambda: '[]')
    asyncio.run(daily.refresh(NOW))
    assert life.snapshot(NOW)['current'] is not None
    assert daily.error_code is None
    # Background life never appraises; the moment waits for the user's next message.
    assert daily.emotion.error_code is None
    asyncio.run(daily.emotion.evaluate_received([receipt()], now=NOW + timedelta(minutes=1)))
    assert daily.emotion.error_code == 'EMOTION_EVALUATION_UNAVAILABLE'
    assert daily.emotion.store.pending_source_ids(before=NOW + timedelta(minutes=1))


def test_life_moments_are_appraised_with_the_next_message_and_guide_the_next_choice(tmp_path):
    from tests.private_world.decisions import life_decision
    class Combined(Gateway):
        def __init__(self):
            super().__init__()
            self.life_calls = []
        async def complete_structured_scoped(self, messages, **kwargs):
            if kwargs['response_format'].get('name') == 'character_emotion':
                return await super().complete_structured_scoped(messages, **kwargs)
            packet = json.loads(messages[-1]['content'])
            self.life_calls.append((packet, messages))
            return SimpleNamespace(text=json.dumps(life_decision(messages)))
    life = DailyLifeStore(tmp_path / 'life.sqlite3')
    life.publish_day('day:existing', {'location': '家里', 'activity': '练琴', 'note': '练习一小段'}, [], occurred_at=NOW)
    gateway = Combined()
    daily = DailyLifeRuntime(life, lambda: gateway, lambda: '[]')
    asyncio.run(daily.refresh(NOW))
    assert gateway.calls == [] and gateway.life_calls == []  # No paid appraisal without the user.
    # The user writes: one evaluation covers the message and the life moment.
    asyncio.run(daily.emotion.evaluate_received([receipt()], now=NOW + timedelta(minutes=1)))
    assert len(gateway.calls) == 1
    kinds = {s['source_kind'] for s in gateway.calls[0][0]['assessment']['sources']}
    assert 'published_world' in kinds
    asyncio.run(daily.refresh(NOW + timedelta(hours=5, minutes=30)))
    assert len(gateway.life_calls) == 1
    decision, messages = gateway.life_calls[0]
    assert decision['emotion']['interpretation_only'] is True
    assert decision['emotion']['reactions'][0]['action_tendency'] == 'rest'
    assert '倾向' in messages[0]['content']
    assert len(gateway.calls) == 1  # The new life moment waits for the next message.


@pytest.mark.parametrize('mistake', ['joined_quote', 'unknown_concern'])
@pytest.mark.parametrize('corrected_concern', [None, 'open'])
def test_published_ongoing_practice_can_repair_rejected_appraisal_once(tmp_path, mistake, corrected_concern):
    # Synthetic replay of day2:emotion:4 / day3:emotion:5: note and progress
    # were joined into a quote; a world project ID was treated as an open concern.
    note = '在家里练琴：左手两处分手慢练。'
    progress = '针对上一日反馈，将问题乐段分开手慢速重组，寻找肌肉记忆点。'
    def evaluate(packet):
        source = packet['assessment']['sources'][0]
        assert source['source_kind'] == 'published_world'
        value = output(packet, reaction='calm')
        item = value['appraisals'][0]
        item.update(quote=note, action_tendency='continue')
        if len(gateway.calls) == 1:
            if mistake == 'joined_quote':
                item['quote'] = note + progress
            else:
                item['concern'] = dict(id='synthetic-piano', action='resolve', summary='改了练习方法')
        else:
            with life._db() as db:
                assert db.execute('SELECT count(*) FROM character_emotion_appraisals').fetchone()[0] == 0
                assert db.execute('SELECT count(*) FROM character_emotion_appraisal_history').fetchone()[0] == 0
            correction = gateway.calls[-1][2][1]['content']
            assert ('quote_not_contiguous' if mistake == 'joined_quote' else 'concern_not_open') in correction
            if corrected_concern:
                item['concern'] = dict(id='concern:' + source['source_id'], action='open', summary='衔接仍在练习')
        return value
    emotion, gateway, life = runtime(tmp_path, Gateway(evaluate))
    life.publish_day('day:practice', dict(location='家里', activity='练琴：左手两处分手慢练', note=note),
                     [dict(id='synthetic-piano', title='左手衔接练习',
                           detail=progress, status='ongoing')], occurred_at=NOW, activity_kind='practice')
    with life._db() as db:
        original_world = tuple(db.execute('SELECT * FROM life_moments').fetchone())
    view = asyncio.run(emotion.refresh_world(NOW))
    assert len(gateway.calls) == 2 and emotion.error_code is None
    assert view['reactions'][0]['reaction'] == 'calm'
    assert bool(view['concerns']) is bool(corrected_concern)
    assert emotion.store.pending_source_ids(before=NOW) == []
    before, after = [call[0]['assessment']['sources'][0] for call in gateway.calls]
    assert before == after  # Including exact text and source_hash, not a rewritten source.
    with life._db() as db:
        assert tuple(db.execute('SELECT * FROM life_moments').fetchone()) == original_world
        assert json.loads(db.execute('SELECT payload FROM life_projects').fetchone()[0])['status'] == 'ongoing'
        assert db.execute('SELECT count(*) FROM character_emotion_appraisals').fetchone()[0] == 1
    assert gateway.calls[0][1]['request_id'] != gateway.calls[1][1]['request_id']


@pytest.mark.parametrize('field', ['quote', 'reported_quote'])
def test_repeated_invalid_quotes_exhaust_two_calls_without_partial_commit(tmp_path, field, capsys):
    marker = 'private-invalid-quote-do-not-log'
    def evaluate(packet):
        value = output(packet)
        item = value['appraisals'][-1]
        if field == 'quote':
            item['quote'] = marker
        else:
            item['reported_affect'] = dict(subject='user', quote=marker, affect='frustrated')
        return value
    emotion, gateway, life = runtime(tmp_path, Gateway(evaluate))
    items = [receipt('first'), receipt('second', '我有点烦，但还在继续练习。')]
    view = asyncio.run(emotion.evaluate_received(items, now=NOW))
    assert len(gateway.calls) == 2
    assert view['reactions'] == [] and view['reported_affects'] == [] and view['pending_current_input']
    assert emotion.error_code == 'EMOTION_EVALUATION_UNAVAILABLE'
    assert set(emotion.store.pending_source_ids(before=NOW)) == {item.source_id for item in items}
    feedback = gateway.calls[1][2][1]['content']
    assert ('quote_not_contiguous' if field == 'quote' else 'reported_quote_not_contiguous') in feedback
    assert marker not in feedback and marker not in repr(emotion.error_code)
    assert marker not in ''.join(capsys.readouterr())
    assert gateway.calls[0][0]['assessment']['sources'] == gateway.calls[1][0]['assessment']['sources']
    with life._db() as db:
        assert db.execute('SELECT count(*) FROM character_emotion_appraisals').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM character_emotion_appraisal_history').fetchone()[0] == 0


@pytest.mark.parametrize('failure', ['json', 'schema', 'provider_schema'])
def test_structure_errors_can_be_repaired_once_from_original_sources(tmp_path, failure):
    from llm_gateway import ProviderProtocolError
    class InvalidFirst(Gateway):
        async def complete_structured_scoped(self, messages, **kwargs):
            if self.calls:
                return await super().complete_structured_scoped(messages, **kwargs)
            packet = json.loads(messages[-1]['content'])
            self.calls.append((packet, kwargs, messages))
            if failure == 'provider_schema':
                raise ProviderProtocolError('structured_validation_failed')
            value = output(packet)
            value['appraisals'][0]['reaction'] = 'invented-value'
            return SimpleNamespace(text='{"appraisals":' if failure == 'json' else json.dumps(value))
    emotion, gateway, _ = runtime(tmp_path, InvalidFirst())
    view = asyncio.run(emotion.evaluate_received([receipt()], now=NOW))
    assert len(gateway.calls) == 2 and emotion.error_code is None
    assert view['reactions'] and not view.get('pending_current_input')
    assert 'invalid_structure' in gateway.calls[1][2][1]['content']
    assert gateway.calls[0][0]['assessment']['sources'] == gateway.calls[1][0]['assessment']['sources']


@pytest.mark.parametrize('failure', ['timeout', 'network', 'truncated', 'protocol', 'storage'])
def test_noncorrectable_failures_stay_pending_without_semantic_retry(tmp_path, monkeypatch, failure):
    from llm_gateway import ProviderProtocolError
    import sqlite3
    def evaluate(packet):
        if failure == 'timeout': raise TimeoutError('private-timeout')
        if failure == 'network': raise OSError('private-network')
        if failure == 'truncated': raise ProviderProtocolError('structured_truncated')
        if failure == 'protocol': raise ProviderProtocolError('invalid_json')
        return output(packet)
    emotion, gateway, _ = runtime(tmp_path, Gateway(evaluate))
    if failure == 'storage':
        def fail_commit(*_):
            raise sqlite3.OperationalError('private-storage')
        monkeypatch.setattr(emotion.store, 'commit', fail_commit)
    view = asyncio.run(emotion.evaluate_received([receipt()], now=NOW))
    assert len(gateway.calls) == 1
    assert view['reactions'] == [] and view['pending_current_input']
    assert emotion.error_code == 'EMOTION_EVALUATION_UNAVAILABLE'


@pytest.mark.parametrize('first', ['stale', 'invalid'])
def test_stale_and_validation_share_total_two_attempt_budget(tmp_path, monkeypatch, first):
    def evaluate(packet):
        value = output(packet)
        kind = first if len(gateway.calls) == 1 else ('invalid' if first == 'stale' else 'stale')
        if kind == 'invalid':
            value['appraisals'][0]['quote'] = '不存在的引文'
        return value
    emotion, gateway, life = runtime(tmp_path, Gateway(evaluate))
    original = emotion.store.commit
    bases = []
    def commit(basis, value):
        bases.append(basis)
        kind = first if len(gateway.calls) == 1 else ('invalid' if first == 'stale' else 'stale')
        if kind == 'stale':
            raise ValueError('EMOTION_CONTEXT_STALE')
        return original(basis, value)
    monkeypatch.setattr(emotion.store, 'commit', commit)
    view = asyncio.run(emotion.evaluate_received([receipt()], now=NOW))
    assert len(gateway.calls) == len(bases) == 2
    assert bases[0] is not bases[1]
    assert view['reactions'] == [] and view['pending_current_input']
    with life._db() as db:
        assert db.execute('SELECT count(*) FROM character_emotion_appraisals').fetchone()[0] == 0


def test_validation_retry_reads_changed_prior_context_instead_of_reusing_old_basis(tmp_path):
    previous = receipt('earlier', '衔接还有一处卡住了。', NOW - timedelta(minutes=1))
    def evaluate(packet):
        value = output(packet)
        if len(gateway.calls) == 1:
            emotion.store.receive(previous.source_id, previous.user_message, occurred_at=previous.occurred_at)
            basis = emotion.store.assessment([previous.source_id], now=NOW)
            emotion.store.commit(basis, output({'assessment': basis}, reaction='concerned'))
            value['appraisals'][0]['quote'] = '没有出现的语句'
        return value
    emotion, gateway, _ = runtime(tmp_path, Gateway(evaluate))
    view = asyncio.run(emotion.evaluate_received([receipt()], now=NOW))
    assert len(gateway.calls) == 2 and not view.get('pending_current_input')
    before, after = [call[0]['assessment'] for call in gateway.calls]
    assert before['context_version'] != after['context_version']
    assert before['sources'] == after['sources']
    assert before['source_contexts'][receipt().source_id]['prior_appraisals'] == []
    assert after['source_contexts'][receipt().source_id]['prior_appraisals'][0]['source_id'] == previous.source_id


def test_later_invalid_concern_rolls_back_earlier_item_before_correction(tmp_path):
    def evaluate(packet):
        value = output(packet)
        if len(gateway.calls) == 1:
            value['appraisals'][-1]['concern'] = dict(
                id='world-project-not-a-concern', action='resolve', summary='尝试了另一种办法')
        else:
            # Unlike a bad quote, this fails after the first item was inserted
            # inside commit's transaction; no provisional row may survive it.
            with life._db() as db:
                assert db.execute('SELECT count(*) FROM character_emotion_appraisals').fetchone()[0] == 0
                assert db.execute('SELECT count(*) FROM character_emotion_appraisal_history').fetchone()[0] == 0
            assert 'concern_not_open' in gateway.calls[-1][2][1]['content']
        return value
    emotion, gateway, life = runtime(tmp_path, Gateway(evaluate))
    items = [receipt('first', '这里还有点卡。', NOW - timedelta(seconds=1)), receipt('second', '换个办法继续练。')]
    view = asyncio.run(emotion.evaluate_received(items, now=NOW))
    assert len(gateway.calls) == 2 and emotion.error_code is None
    assert not view.get('pending_current_input')
    with life._db() as db:
        assert db.execute('SELECT count(*) FROM character_emotion_appraisals').fetchone()[0] == 2
