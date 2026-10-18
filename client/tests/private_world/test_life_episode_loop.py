"""A life episode answer that cannot be applied must not be asked for again every minute."""
import asyncio
import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from runtime.private_world import daily_life_runtime, day_plan, life_episode, meal_lifecycle, project_title
from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime
from runtime.private_world.jev_exchange import _assemble_updates
from runtime.reply import jev_questions
from runtime.reply.companion_decision import CompanionDecisionError, _http_error_code

LOCAL = timezone(timedelta(hours=8))
GOOD_ID = 'jev.' + 'a1' * 20
FRAGMENT_ID = 'jev.99b92e2f' + 'c3' * 14
FRAGMENT_REPLY = '好，我自己慢慢来，也不用，你别担心。'


def at(hour, minute=0):
    return datetime(2026, 10, 18, hour, minute, tzinfo=LOCAL)  # a Sunday: no classes


class Port:
    """Answers the world decision and the episode like the cloud does, and counts the asks."""

    def __init__(self, *, refuse_episode=False, fail_decision=False):
        self.decisions, self.episodes = [], []
        self.refuse_episode, self.fail_decision = refuse_episode, fail_decision

    async def ask(self, state, questions, *, purpose):
        if purpose == 'world-decision':
            self.decisions.append(state['context']['time'])
            if self.fail_decision:
                raise ValueError('JEV_RESPONSE_INVALID')
            answers = {}
            for key, question in questions.items():
                criteria = question['criteria']
                if key == 'activity':
                    answers[key] = next(k for k in criteria if k.startswith('creative'))
                elif key == 'project':
                    answers[key] = next((k for k, v in criteria.items()
                                         if isinstance(v, dict) and v.get('id') in {GOOD_ID, FRAGMENT_ID}), 'none')
                elif key == 'project_outcome':
                    answers[key] = 'ongoing'
                elif key.endswith('_scope'):
                    answers[key] = 'open'
                else:
                    answers[key] = next(iter(criteria))
            return answers
        assert purpose == 'world-life-episode'
        self.episodes.append(state['context']['time'])
        if self.refuse_episode:
            raise CompanionDecisionError('JEV_LIFE_EPISODE_REPEATED')
        path = 'plan_0' if 'plan_0' in state['paths'] else next(iter(state['paths']))
        return {'trigger': 'own_activity', 'experience': f'{path}:progress:none'}


def world(tmp_path, monkeypatch, port, *, fragment=True, apply_error=ValueError('DAILY_LIFE_DECISION_PROJECT_REOPEN')):
    """A stale world: a creative decision is due on project GOOD_ID, publishing a project update fails."""
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    reply = '我会继续把和声草稿的提纲写完。'
    store.record_exchange('reply:good', '不用急', reply, [dict(
        id=GOOD_ID, title='和声草稿提纲', detail=reply, quote=reply, status='ongoing', kind='linli', actor='linli')],
        occurred_at=at(9))
    if fragment:
        store.record_exchange('reply:fragment', '你别太累', FRAGMENT_REPLY, [dict(
            id=FRAGMENT_ID, title='也不用，', detail=FRAGMENT_REPLY, quote='也不用，', status='ongoing',
            kind='linli', actor='linli')], occurred_at=at(10))
    store.publish_day('day:reading', dict(location='家里', activity='阅读', note='在家阅读。'), [],
                      occurred_at=at(17), activity_kind='reading')
    path = lambda outcome, status: {'obstacle': '想法还不够清楚', 'response': '先做个粗略版本',
                                    'status': status, 'outcome': outcome}
    plan = {'activities': {'creative': [{'focus': '和声草稿', 'paths': [
        path('压成两页提纲，条目归成了三类', 'completed'), path('只写了一半提纲', 'partial'),
        path('这次没写出来', 'failed')]}]},
        'meals': {'breakfast': ['豆浆'], 'lunch': ['面'], 'dinner': ['粥'], 'snack': ['水果']}}
    with store._db() as db:
        day_plan.initialize(db)
        db.execute('INSERT OR REPLACE INTO life_day_plans VALUES (?,?)', ('2026-10-18', json.dumps(plan, ensure_ascii=False)))

    async def no_meals(*args, **kwargs):
        return None

    monkeypatch.setattr(meal_lifecycle, 'advance', no_meals)
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    monkeypatch.setattr(daily_life_runtime, 'configured_duties', lambda: None)
    real_publish = store.publish_day

    def publish(source_id, current, projects, **kwargs):
        if apply_error is not None and projects:
            raise apply_error
        return real_publish(source_id, current, projects, **kwargs)

    store.publish_day = publish
    return store, DailyLifeRuntime(store, lambda: None, lambda: '[]')


def ticks(runtime, store, start, minutes, *, letters=False):
    async def run():
        for minute in range(minutes):
            now = start + timedelta(minutes=minute)
            if letters:
                source = f'reply:letter{minute}'
                store.record_exchange(source, '嗯', f'我在写提纲{minute}。', [], occurred_at=now - timedelta(seconds=30))
                with store._db() as db:
                    db.execute('INSERT OR REPLACE INTO life_exchange_world_gate VALUES (?,?,?)', (source, 'reconsider', 'x'))
            await runtime.refresh(now)
    asyncio.run(run())


def project(store, now, project_id):
    return next(p for p in store.snapshot(now)['projects'] if p['id'] == project_id)


def test_an_answer_that_cannot_be_applied_is_asked_for_twice_then_she_simply_stops(tmp_path, monkeypatch):
    port = Port()
    store, runtime = world(tmp_path, monkeypatch, port)
    ticks(runtime, store, at(18, 47), 40)
    # Two attempts that ask, five minutes apart; the third publishes the neutral stop without a third answer.
    assert port.episodes == [at(18, 47).isoformat(), at(18, 52).isoformat()]
    assert port.decisions == [at(18, 47).isoformat(), at(18, 52).isoformat(), at(18, 57).isoformat()]
    later = at(19, 27)
    state = store.snapshot(later)
    assert state['stale'] is False and state['current']['activity_kind'] == 'creative'
    with store._db() as db:
        stored = [json.loads(row[0]) for row in db.execute('SELECT payload FROM life_episodes')]
    assert [item['result']['status'] for item in stored] == ['paused']
    assert project(store, later, GOOD_ID)['status'] == 'ongoing'  # nothing about the project was changed
    assert runtime.error_code is None


def test_a_fresh_letter_every_minute_does_not_make_each_failure_a_first_failure(tmp_path, monkeypatch):
    port = Port()
    store, runtime = world(tmp_path, monkeypatch, port)
    ticks(runtime, store, at(18, 47), 25, letters=True)
    # Two answers that failed, the neutral stop on the third attempt. Letters that arrive
    # afterwards are new situations and are decided on their own, but the failing answer is not asked again.
    assert port.episodes == [at(18, 47).isoformat(), at(18, 52).isoformat()]
    assert port.decisions[:3] == [at(18, 47).isoformat(), at(18, 52).isoformat(), at(18, 57).isoformat()]
    assert store.snapshot(at(19, 12))['current']['activity_kind'] == 'creative'


@pytest.mark.parametrize('error', [AttributeError('synthetic'), IndexError('synthetic'), ZeroDivisionError('synthetic')])
def test_an_unexpected_error_after_the_answer_is_recorded_like_any_other_failure(tmp_path, monkeypatch, error):
    port = Port()
    store, runtime = world(tmp_path, monkeypatch, port, apply_error=error)
    ticks(runtime, store, at(18, 47), 5)
    assert len(port.decisions) == 1 and len(port.episodes) == 1
    assert runtime.error_code == 'DAILY_LIFE_GENERATION_UNAVAILABLE'
    with store._db() as db:
        row = db.execute('SELECT failure_count, retry_after, late_failures FROM daily_life_refresh_retry').fetchone()
    assert row[0] == 1 and row[2] == 1
    assert datetime.fromisoformat(row[1]) >= at(18, 52)


def test_the_servers_refusal_of_a_repeated_ask_is_not_retried_and_the_world_moves_on(tmp_path, monkeypatch):
    port = Port(refuse_episode=True)
    store, runtime = world(tmp_path, monkeypatch, port, apply_error=None)
    ticks(runtime, store, at(18, 47), 4)
    assert len(port.episodes) == 1 and runtime._last_failure_code == 'JEV_LIFE_EPISODE_REPEATED'
    assert runtime._retry_after >= at(18, 52)
    ticks(runtime, store, at(18, 51), 40)
    assert len(port.episodes) == 1  # no second ask for the refused answer
    assert store.snapshot(at(19, 30))['stale'] is False
    assert runtime.error_code is None


def test_the_refusal_reason_is_a_known_error_code():
    body = io.BytesIO(json.dumps({'error': 'life_episode_repeated'}).encode())
    error = urllib.error.HTTPError('https://example.invalid/semantic-decisions', 409, 'refused', {}, body)
    assert _http_error_code(error) == 'JEV_LIFE_EPISODE_REPEATED'
    assert CompanionDecisionError('JEV_LIFE_EPISODE_REPEATED').code == 'JEV_LIFE_EPISODE_REPEATED'


def test_repeated_failures_back_off_to_more_than_half_an_hour_and_stop_within_the_block(tmp_path, monkeypatch):
    port = Port(fail_decision=True)
    store, runtime = world(tmp_path, monkeypatch, port, apply_error=None)
    ticks(runtime, store, at(18, 0), 300, letters=True)
    attempts = [datetime.fromisoformat(stamp) for stamp in port.decisions]
    assert 3 <= len(attempts) <= 6
    gaps = [later - earlier for earlier, later in zip(attempts, attempts[1:])]
    assert gaps == sorted(gaps) and gaps[-1] >= timedelta(minutes=30)


def test_a_stuck_project_with_a_piece_of_conversation_as_its_name_is_never_offered_again(tmp_path, monkeypatch):
    port = Port()
    store, runtime = world(tmp_path, monkeypatch, port, apply_error=None)
    shown, _ = store.world_projects(at(18, 47))
    assert [p['id'] for p in shown] == [GOOD_ID]
    # The stuck project stays in the journal and in the letters she is asked about.
    assert FRAGMENT_ID in {p['id'] for p in store.snapshot(at(18, 47))['projects']}
    ticks(runtime, store, at(18, 47), 3)
    assert len(port.decisions) == 1 and len(port.episodes) == 1
    assert project(store, at(18, 50), GOOD_ID)['status'] == 'ongoing'
    assert project(store, at(18, 50), FRAGMENT_ID)['status'] == 'ongoing'


@pytest.mark.parametrize('text', ['也不用，', '好，', '嗯', '不用了', '就是', '…',
                                  '谁骗你了，让你闭眼是怕你刚醒又胡思乱想，…', '还没，'])
def test_pieces_of_conversation_are_not_names(text):
    assert project_title.usable(text) is False


@pytest.mark.parametrize('text', ['练琴', '看书', '和声', '和同学一起排练', '还要写作业', '明天练好后，发给你听。',
                                  '我明天去买菜，', '信也寄了。', '碗洗了。', '给你录一段钢琴曲', 'Complete concert'])
def test_real_names_are_kept(text):
    assert project_title.usable(text) is True


def candidate(text, support):
    return {'anchor': {'source': 'linli_reply', 'text': text, 'support': support, 'span': [0, len(text)]},
            'old': None, 'applicability': 'linli', 'status': 'ongoing', 'quote': text}


def test_an_update_located_only_by_a_remnant_of_a_sentence_is_not_recorded_as_a_project():
    slots, _ = _assemble_updates([candidate('也不用，', FRAGMENT_REPLY)], 'life:remnant', False)
    assert slots == []
    slots, _ = _assemble_updates([candidate('我会把提纲写完。', '我会把提纲写完。')], 'life:name', False)
    assert [entries[0]['item']['title'] for _, entries in slots] == ['我会把提纲写完。']


def test_an_existing_project_keeps_the_name_it_has():
    old = {'id': GOOD_ID, 'title': '也不用，', 'kind': 'linli', 'status': 'ongoing'}
    slots, _ = _assemble_updates([{**candidate('也不用，', FRAGMENT_REPLY), 'old': old}], 'life:old', False)
    assert [entries[0]['item']['title'] for _, entries in slots] == ['也不用，']


def neutral(kind, **context):
    class Silent:
        async def ask(self, *args, **kwargs):
            raise AssertionError('the neutral outcome is never asked for')
    base = {**dict(time=at(15).isoformat(), world={}, rhythm={}, day_plan=None), **context}
    return asyncio.run(life_episode.create(Silent(), 'day:neutral', at(15), kind, base, fallback=True))


@pytest.mark.parametrize('kind', ['practice', 'reading', 'walk', 'errand', 'housework', 'shopping'])
def test_the_neutral_outcome_only_stops_for_now(kind):
    value = neutral(kind)
    assert value['result']['status'] == 'paused' and value['interpretation']['subjective'] is True
    assert value['effects']['next_action'] is None and value['effects']['open_loop'] is None


def test_the_neutral_outcome_follows_the_days_own_pause_path():
    plan = {'activities': {'creative': [{'focus': '和声草稿', 'paths': [
        {'obstacle': '想法还不够清楚', 'response': '先做个粗略版本', 'status': 'completed', 'outcome': '压成两页提纲'},
        {'obstacle': '想法还不够清楚', 'response': '先做个粗略版本', 'status': 'partial', 'outcome': '只写了一半'}]}]}}
    value = neutral('creative', day_plan=plan, selected_activity={'kind': 'creative', 'focus': '和声草稿'})
    assert value['result']['status'] == 'paused'


@pytest.mark.parametrize('kind', ['rest', 'class', 'creative'])
def test_without_a_neutral_way_to_stop_nothing_is_invented(kind):
    # Rest carries recovery and sleep, class needs a current course, and creative has no plan to stop from.
    context = dict(world={'schedule': {'current_class': {'title': '和声学'}}}) if kind == 'class' else {}
    assert neutral(kind, **context) is None


@pytest.mark.parametrize('kind', ['bath_started', 'bath_finished'])
def test_a_bath_keeps_its_published_meaning_in_the_neutral_outcome(kind):
    value = neutral(kind)
    assert value['activity_kind'] == kind and value['result']['status'] == ('partial' if kind == 'bath_started' else 'completed')


def test_opening_the_world_page_does_not_re_decide_for_a_project_that_is_never_offered(tmp_path, monkeypatch):
    port = Port()
    store, runtime = world(tmp_path, monkeypatch, port, apply_error=None)
    with store._db() as db:
        db.execute("DELETE FROM life_moments WHERE source_id='reply:good'")  # only the remnant is left
    store.publish_day('day:fresh', dict(location='家里', activity='阅读', note='在家阅读。'), [],
                      occurred_at=at(18, 30), activity_kind='reading')
    assert any(p['id'] == FRAGMENT_ID and p['time_scope_pending'] for p in store.snapshot(at(18, 47))['projects'])
    asyncio.run(runtime.refresh(at(18, 47), recheck_projects=True))
    assert port.decisions == []


def test_a_retry_table_from_an_earlier_client_is_upgraded_in_place(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    with store._db() as db:
        db.execute('DROP TABLE IF EXISTS daily_life_refresh_retry')
        db.execute('CREATE TABLE daily_life_refresh_retry (source_id TEXT PRIMARY KEY, failure_count INTEGER NOT NULL, retry_after TEXT NOT NULL)')
        db.execute("INSERT INTO daily_life_refresh_retry VALUES ('day:20261018:3', 2, '2026-10-18T18:57:00+08:00')")
    runtime = DailyLifeRuntime(store, lambda: None, lambda: '[]')
    assert runtime._refresh_retry_state('day:20261018:3') == (2, at(18, 57))
    assert runtime._refresh_late_failures('day:20261018:3') == 0
