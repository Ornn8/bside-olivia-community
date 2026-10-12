"""One small request per delivered exchange: it changes only what the exchange changed."""
import json


from runtime.diary.diary import WRITE_AFTER_HOUR
from runtime.private_world import day_schedule, world_check
from tests.private_world.day_world import MONDAY, Port, at, exchange, make_world, moments, tick


def checking(detail=None, **overrides):
    """Answers the check as given (a value may compute from the request) and the day-schedule request plainly."""

    def policy(state, questions, purpose):
        if purpose == 'exchange-world-check':
            return {key: (lambda v: v(state, questions) if callable(v) else v)(overrides.get(key, 'none')) for key in questions}
        if purpose == 'exchange-world-detail' and detail is not None:
            return detail(state, questions)
        if purpose == 'world-decision':
            rest = next(k for k, v in questions['activity']['criteria'].items() if v['kind'] == 'rest')
            return {key: (rest if key == 'activity' else 'none' if key in {'project', 'next_activity'} else next(iter(q['criteria'])))
                    for key, q in questions.items()}

    return policy


def laid_out(tmp_path, monkeypatch, **kwargs):
    store, runtime, port = make_world(tmp_path, monkeypatch, Port(checking(**kwargs)))
    tick(runtime, at(WRITE_AFTER_HOUR, 0))
    return store, runtime, port


def plan(store):
    return day_schedule.load(store, MONDAY)


def item(store, item_id):
    return next(i for i in plan(store)['items'] if i['id'] == item_id)


def gate(store, n):
    with store._db() as db:
        row = db.execute('SELECT decision FROM life_exchange_world_gate WHERE source_id=?', (f'reply:t{n}:1',)).fetchone()
    return row[0] if row else None


def now_block(state, questions):
    return next(k for k, v in questions['target']['criteria'].items() if '正在进行' in v)


def walk(state, questions):
    return next(k for k in questions['activity']['criteria'] if k.startswith('walk'))


def test_no_change_leaves_the_schedule_and_the_world_as_they_are(tmp_path, monkeypatch):
    store, runtime, port = laid_out(tmp_path, monkeypatch)
    tick(runtime, at(10, 41))
    before, seen = plan(store), len(moments(store))
    exchange(runtime, 1, at(10, 50), '在做什么呢', '在看书，下午还有钢琴课。')
    assert plan(store) == before and len(moments(store)) == seen
    assert gate(store, 1) == 'none' and runtime.refreshes == []
    assert port.purposes() == ['world-day-schedule', 'exchange-world-check']


def test_the_request_holds_the_original_words_and_the_world_status_only(tmp_path, monkeypatch):
    store, runtime, port = laid_out(tmp_path, monkeypatch)
    tick(runtime, at(10, 41))
    exchange(runtime, 1, at(10, 50), '今晚想吃什么', '还没想好。')
    state = port.calls[-1]['state']
    assert set(state) == {'contract', 'exchange', 'current', 'meals_today', 'projects', 'rest_of_today', 'quotes', 'quote_contract'}
    assert state['exchange']['user_text'] == '今晚想吃什么' and state['exchange']['reply_text'] == '还没想好。'
    assert state['current']['activity'].startswith(('练琴', '阅读', '创作', '整理', '散步', '办日常', '买日常', '休息'))
    assert {row['slot'] for row in state['meals_today']} == {'breakfast', 'lunch', 'dinner'}
    assert all(set(row) == {'id', 'time', 'fixed'} for row in state['rest_of_today'])
    assert len(json.dumps(port.calls[-1], ensure_ascii=False).encode()) < 12000


def test_the_current_activity_ends_and_is_replaced_by_a_candidate(tmp_path, monkeypatch):
    store, runtime, port = laid_out(tmp_path, monkeypatch, change='replace', target=now_block, activity=walk)
    tick(runtime, at(10, 41))
    exchange(runtime, 1, at(10, 50), '出去走走吗', '好，我现在就去散步。')
    changed = item(store, 'i2')
    assert changed['rev'] == 1 and changed['result']['activity']['kind'] == 'walk'
    assert gate(store, 1) == 'adjusted' and runtime.refreshes
    tick(runtime, at(10, 51))
    assert moments(store)[-1]['activity_kind'] == 'walk' and moments(store)[-1]['source_id'] == f'sched:{MONDAY}:i2:1'
    assert port.purposes() == ['world-day-schedule', 'exchange-world-check']  # still nothing asked for the time passing


def test_a_later_block_is_cancelled_into_rest_or_swapped_with_another(tmp_path, monkeypatch):
    store, runtime, port = laid_out(tmp_path, monkeypatch)
    free = [i['id'] for i in plan(store)['items'] if i['type'] == 'free']
    first, second = free[-3], free[-2]
    old_first, old_second = item(store, first)['result'], item(store, second)['result']
    port.answer = checking(change='move', target=first, partner=second)
    exchange(runtime, 1, at(9, 30), '练琴改到晚上吧', '好，那我先做别的，晚点再练。')
    assert item(store, first)['result'] == old_second and item(store, second)['result'] == old_first
    assert item(store, first)['rev'] == item(store, second)['rev'] == 1
    port.answer = checking(change='replace', target=free[-1], activity=lambda s, q: next(k for k in q['activity']['criteria'] if k.startswith('rest')))
    exchange(runtime, 2, at(11, 10), '晚上不用出门了', '那我晚上在家休息。')
    assert item(store, free[-1])['result']['activity']['kind'] == 'rest' and gate(store, 2) == 'adjusted'


def test_a_meal_she_says_she_finished_ends_now_and_the_next_block_starts(tmp_path, monkeypatch):
    store, runtime, port = laid_out(tmp_path, monkeypatch, change='meal', meal='finished_lunch')
    tick(runtime, at(12, 0))
    tick(runtime, at(12, 5))
    exchange(runtime, 1, at(12, 10), '吃完了吗', '吃完了，碗也洗了。')
    lunch = [m for m in store.snapshot(at(12, 11))['world']['meals'] if m['slot'] == 'lunch']
    assert [m['status'] for m in lunch] == ['eaten'] and lunch[0]['finished_at'] == at(12, 10).isoformat()
    tick(runtime, at(12, 11))
    assert moments(store)[-1]['source_id'].startswith(f'sched:{MONDAY}:') and moments(store)[-1]['activity_kind'] != 'meal'
    assert gate(store, 1) == 'adjusted'


def test_a_skipped_dinner_leaves_its_hour_to_rest(tmp_path, monkeypatch):
    store, runtime, port = laid_out(tmp_path, monkeypatch, change='meal', meal='skip_dinner')
    exchange(runtime, 1, at(17, 0), '晚饭吃什么', '今晚不吃了，早点休息。')
    tick(runtime, at(18, 1))
    assert [m['status'] for m in store.snapshot(at(18, 2))['world']['meals'] if m['slot'] == 'dinner'] == ['skipped']
    assert moments(store)[-1]['activity_kind'] == 'rest' and moments(store)[-1]['source_id'].endswith('r:0')


def test_something_no_offered_option_covers_goes_back_to_one_live_decision(tmp_path, monkeypatch):
    store, runtime, port = laid_out(tmp_path, monkeypatch, change='outside')
    tick(runtime, at(10, 41))
    exchange(runtime, 1, at(10, 50), '陪我打一会儿游戏吧', '好呀，那我陪你玩一会儿。')
    assert gate(store, 1) == 'reconsider' and runtime.refreshes
    tick(runtime, at(10, 51))
    assert port.count('world-decision') == 1
    tick(runtime, at(10, 52))
    assert port.count('world-decision') == 1  # once, not every minute
    assert not moments(store)[-1]['source_id'].startswith('sched:')


def test_a_replacement_the_contract_refuses_is_not_stored(tmp_path, monkeypatch):
    store, runtime, port = laid_out(tmp_path, monkeypatch, change='replace', target=now_block, activity=walk)
    tick(runtime, at(10, 41))

    def refuse(value, data):
        raise ValueError('DAILY_LIFE_DECISION_CLASS_CONFLICT')

    monkeypatch.setattr(world_check, 'compile_decision', refuse)
    before = plan(store)
    exchange(runtime, 1, at(10, 50), '出去走走吗', '好，我现在就去散步。')
    assert plan(store) == before and gate(store, 1) == 'reconsider'


def test_a_user_who_only_asks_is_not_taken_as_a_change(tmp_path, monkeypatch):
    # The model reads the words; the request tells it that an unanswered invitation changes nothing.
    store, runtime, port = laid_out(tmp_path, monkeypatch)
    exchange(runtime, 1, at(9, 30), '要不要出来走走', '我再看看吧，现在有点忙。')
    contract = port.calls[-1]['state']['contract']
    assert '询问、邀请、建议' in contract and '都不改变安排' in contract
    criteria = port.calls[-1]['questions']['change']['criteria']
    assert all(set(value) == {'what', 'not_for', 'examples'} for value in criteria.values())


# The plan the two of them agree on is taken from their own words, whatever the day's candidates were.
USER = '今晚我们去外面吃老鸭汤配阳春面吧，就去巷口的老店。'
REPLY = '好呀，那就这么定了。'


def agreed_dinner(user=USER, reply=REPLY, mode='dine_out', place_id='shop', place='巷口的老店', food='老鸭汤配阳春面'):
    def spans(sentence, text):
        start = sentence.index(text)
        return str(start), str(start + len(text))

    def detail(state, questions):
        sentence = state['sentence']
        answers = {key: 'none' for key in questions}
        answers['value_start'], answers['value_end'] = spans(sentence, food)
        if place:
            answers['place_start'], answers['place_end'] = spans(sentence, place)
        answers.update(mode=mode, place_id=place_id, when='none', hour='unknown', minute='unknown')
        return answers

    return dict(change='meal', meal='agreed_dinner', agreement='u0', detail=detail), user, reply


def test_an_agreed_dish_outside_the_candidates_is_taken_exactly_from_the_words(tmp_path, monkeypatch):
    overrides, user, reply = agreed_dinner()
    store, runtime, port = laid_out(tmp_path, monkeypatch, **overrides)
    exchange(runtime, 1, at(16, 0), user, reply)
    dinner = next(i for i in plan(store)['items'] if i['type'] == 'meal' and i['slot'] == 'dinner')
    assert dinner['entry']['food'] == '老鸭汤配阳春面' and dinner['entry']['mode'] == 'dine_out'
    assert dinner['entry']['place']['place_id'] == 'shop' and dinner['entry']['place']['name'] == '巷口的老店'
    # Every value is a verbatim span of the original text, recorded with its source.
    evidence = dinner['agreed']
    assert evidence['source'] == 'user_letter' and evidence['sentence'] == user
    assert user[slice(*evidence['value'])] == '老鸭汤配阳春面' and user[slice(*evidence['place'])] == '巷口的老店'
    assert gate(store, 1) == 'adjusted'
    assert [call['purpose'] for call in port.calls[-2:]] == ['exchange-world-check', 'exchange-world-detail']
    assert day_schedule.agreements(store, MONDAY)['dinner']['entry'] == dinner['entry']


def test_the_agreed_dish_and_place_are_what_the_world_publishes_and_the_photo_and_video_read(tmp_path, monkeypatch):
    overrides, user, reply = agreed_dinner()
    store, runtime, port = laid_out(tmp_path, monkeypatch, **overrides)
    exchange(runtime, 1, at(16, 0), user, reply)
    tick(runtime, at(18, 0))
    assert moments(store)[-1]['activity'] == '准备出门就餐晚餐' and '老鸭汤配阳春面' in moments(store)[-1]['note']
    tick(runtime, at(18, 21))
    eating = moments(store)[-1]
    assert eating['activity'] == '正在吃晚餐' and eating['note'] == '正在吃：老鸭汤配阳春面。'
    assert eating['location'] == '巷口的老店' and eating['place']['name'] == '巷口的老店'
    meal = next(m for m in store.snapshot(at(18, 22))['world']['meals'] if m['slot'] == 'dinner')
    assert (meal['food'], meal['mode'], meal['status'], meal['place']['name']) == ('老鸭汤配阳春面', 'dine_out', 'eating', '巷口的老店')
    # What a photo is placed by, and what a daily video is authored from, are this same record.
    assert store.observed_place(now=at(18, 22))['place']['name'] == '巷口的老店'
    sources = [s for s in store.daily_video_sources(now=at(18, 22), moment=True) if s['event_kind'] == 'meal']
    assert sources and '老鸭汤配阳春面' in sources[0]['detail'] and sources[0]['place']['name'] == '巷口的老店'
    facts = store.daily_video_event_facts({'event_id': sources[0]['event_id'], 'event_kind': 'meal'})
    assert '老鸭汤配阳春面' in facts['note'] and facts['place']['name'] == '巷口的老店'
    assert port.count('world-meal-lifecycle') == 0 and port.count('world-decision') == 0


def test_a_later_change_of_mind_about_an_agreed_dinner_replaces_the_agreement(tmp_path, monkeypatch):
    overrides, user, reply = agreed_dinner()
    store, runtime, port = laid_out(tmp_path, monkeypatch, **overrides)
    exchange(runtime, 1, at(16, 0), user, reply)
    assert 'dinner' in day_schedule.agreements(store, MONDAY)
    port.answer = checking(change='meal', meal='skip_dinner')
    exchange(runtime, 2, at(16, 30), '算了，今晚别出去了', '好，那我今晚就不吃了。')
    assert day_schedule.agreements(store, MONDAY) == {}
    tick(runtime, at(18, 1))
    assert [m['status'] for m in store.snapshot(at(18, 2))['world']['meals'] if m['slot'] == 'dinner'] == ['skipped']


def test_an_agreed_dish_is_honoured_even_when_the_day_could_not_be_laid_out(tmp_path, monkeypatch):
    overrides, user, reply = agreed_dinner()
    answer = checking(**overrides)

    def policy(state, questions, purpose):
        if purpose == 'world-day-schedule':
            raise RuntimeError('JEV_UNAVAILABLE')
        return answer(state, questions, purpose)

    store, runtime, port = make_world(tmp_path, monkeypatch, Port(policy))
    tick(runtime, at(WRITE_AFTER_HOUR, 0))
    assert plan(store) is None
    tick(runtime, at(16, 0))
    exchange(runtime, 1, at(16, 5), user, reply)
    assert gate(store, 1) == 'adjusted'
    tick(runtime, at(18, 0))
    tick(runtime, at(18, 21))
    dinner = next(m for m in store.snapshot(at(18, 22))['world']['meals'] if m['slot'] == 'dinner')
    assert (dinner['food'], dinner['mode'], dinner['place']['name']) == ('老鸭汤配阳春面', 'dine_out', '巷口的老店')
    assert moments(store)[-1]['note'] == '正在吃：老鸭汤配阳春面。'
    assert not any(call['purpose'] == 'world-meal-lifecycle' and call['state']['slot'] == 'dinner' for call in port.calls)


def test_a_later_unrelated_message_leaves_the_agreed_dinner_unchanged(tmp_path, monkeypatch):
    overrides, user, reply = agreed_dinner()
    store, runtime, port = laid_out(tmp_path, monkeypatch, **overrides)
    exchange(runtime, 1, at(16, 0), user, reply)
    before = next(i for i in plan(store)['items'] if i['type'] == 'meal' and i['slot'] == 'dinner')
    port.answer = checking()
    exchange(runtime, 2, at(16, 30), '今天累不累', '还好，下午看了会儿书。')
    assert next(i for i in plan(store)['items'] if i['type'] == 'meal' and i['slot'] == 'dinner') == before
    assert gate(store, 2) == 'none'
    tick(runtime, at(18, 0))
    tick(runtime, at(18, 21))
    assert moments(store)[-1]['note'] == '正在吃：老鸭汤配阳春面。'


def test_a_meal_already_eaten_is_not_replaced_by_a_dish_agreed_later(tmp_path, monkeypatch):
    store, runtime, port = laid_out(tmp_path, monkeypatch)
    for when in (at(12, 0), at(12, 50)):
        tick(runtime, when)
    lunch = next(m for m in store.snapshot(at(12, 50))['world']['meals'] if m['slot'] == 'lunch')
    assert lunch['status'] == 'eaten'
    overrides, _, _ = agreed_dinner(food='红烧排骨饭')

    def wants_lunch(state, questions, purpose):
        if purpose == 'exchange-world-check':
            options = questions.get('meal', {}).get('criteria', {})
            assert not any(key.endswith('_lunch') for key in options)  # not even offered
            return {key: 'none' for key in questions}

    port.answer = wants_lunch
    exchange(runtime, 1, at(13, 30), '中午其实该吃红烧排骨饭的', '是呀，不过已经吃完了。')
    after = next(m for m in store.snapshot(at(13, 31))['world']['meals'] if m['slot'] == 'lunch')
    assert (after['food'], after['status']) == (lunch['food'], 'eaten')
    assert 'lunch' not in day_schedule.agreements(store, MONDAY)
    # Even if the model asked for it anyway, an eaten meal is never rewritten.
    port.answer = checking(change='meal', meal='agreed_lunch', agreement='u0')
    assert world_check._apply_agreed_meal(store, plan(store), None, 'lunch', at(13, 40), {'lunch': lunch}, {'entry': '红烧排骨饭', 'start': None, 'evidence': {}}) is False


def test_a_value_not_found_in_the_words_is_never_accepted(tmp_path, monkeypatch):
    def broken(state, questions):
        answers = {key: 'none' for key in questions}
        answers.update(mode='dine_out', place_id='shop', when='none', hour='unknown', minute='unknown')
        answers['value_start'] = '3'  # a start without an end: unusable
        return answers

    overrides, user, reply = agreed_dinner()
    overrides['detail'] = broken
    store, runtime, port = laid_out(tmp_path, monkeypatch, **overrides)
    exchange(runtime, 1, at(16, 0), user, reply)
    assert gate(store, 1) == 'reconsider'
    assert day_schedule.agreements(store, MONDAY) == {}
    assert next(i for i in plan(store)['items'] if i['type'] == 'meal' and i['slot'] == 'dinner')['entry']['food'] != '老鸭汤配阳春面'


def test_an_agreed_time_moves_the_meal_and_is_bound_to_the_same_sentence(tmp_path, monkeypatch):
    user = '晚上七点半我们去巷口的老店吃老鸭汤配阳春面吧。'
    overrides, _, reply = agreed_dinner(user=user)

    def detail(state, questions):
        sentence = state['sentence']
        answers = {key: 'none' for key in questions}
        food, place = sentence.index('老鸭汤配阳春面'), sentence.index('巷口的老店')
        answers.update(value_start=str(food), value_end=str(food + 7), place_start=str(place), place_end=str(place + 5),
                       mode='dine_out', place_id='shop', when='clock', hour='19', minute='30')
        return answers

    overrides['detail'] = detail
    store, runtime, port = laid_out(tmp_path, monkeypatch, **overrides)
    exchange(runtime, 1, at(16, 0), user, reply)
    dinner = next(i for i in plan(store)['items'] if i['type'] == 'meal' and i['slot'] == 'dinner')
    assert day_schedule.at(dinner['start']) == at(19, 30)
    tick(runtime, at(18, 0))
    assert moments(store)[-1]['activity_kind'] != 'meal'  # not before the agreed time
    tick(runtime, at(19, 0))
    tick(runtime, at(19, 31))
    assert moments(store)[-1]['activity'] == '准备出门就餐晚餐'


def test_what_she_will_do_is_taken_from_the_words_too(tmp_path, monkeypatch):
    user = '你现在去阳台收一下衣服吧。'

    def detail(state, questions):
        sentence = state['sentence']
        answers = {key: 'none' for key in questions}
        start = sentence.index('阳台收一下衣服')
        answers.update(value_start=str(start), value_end=str(start + 7), kind='housework', place_id='unclear')
        return answers

    store, runtime, port = laid_out(tmp_path, monkeypatch, change='replace', target=now_block, activity='agreed', agreement='u0', detail=detail)
    tick(runtime, at(10, 41))
    exchange(runtime, 1, at(10, 50), user, '好，我这就去。')
    changed = item(store, 'i2')
    assert changed['result']['activity']['kind'] == 'housework' and changed['result']['activity']['focus'] == '阳台收一下衣服'
    assert user[slice(*changed['agreed']['value'])] == '阳台收一下衣服'
    tick(runtime, at(10, 51))
    assert moments(store)[-1]['note'] == '在家里整理家务：阳台收一下衣服。'


def test_a_text_too_long_to_quote_exactly_offers_no_agreed_option(tmp_path, monkeypatch):
    store, runtime, port = laid_out(tmp_path, monkeypatch)
    exchange(runtime, 1, at(16, 0), '今天的事。' * 2000, '好。')
    questions = port.calls[-1]['questions']
    assert 'agreement' not in questions and 'quotes' not in port.calls[-1]['state']
    assert not any(key.startswith('agreed') for key in questions.get('meal', {}).get('criteria', {}))


def test_a_commitment_made_in_the_exchange_gets_its_time_scope_in_the_same_request(tmp_path, monkeypatch):
    store, runtime, port = laid_out(tmp_path, monkeypatch, project_time_0_scope='bounded', project_time_0_day='1',
                                    project_time_0_hour='7', project_time_0_minute='0')
    user, reply = '明天早上记得练琴哦', '好，明天早上七点前我把左手练好。'
    store.record_exchange('reply:t1:1', user, reply, [dict(id='promise-1', title='练好左手', detail=reply, status='planned',
                          kind='linli', actor='linli', quote='明天早上七点前我把左手练好')], occurred_at=at(20, 0))
    exchange(runtime, 1, at(20, 0), user, reply)
    assert port.count('exchange-world-check') == 1  # one request carries both the change and the time scope
    state = port.calls[-1]['state']
    assert state['project_time_contexts']['p0']['reply_text'] == reply
    with store._db() as db:
        timings = [json.loads(row[0]) for row in db.execute('SELECT payload FROM life_project_timing')]
    assert [(t['id'], t['scope'], t['deadline_at']) for t in timings] == [('promise-1', 'bounded', at(7, 0, '2026-10-13').isoformat())]
    # Nothing is asked about a commitment that already has its time scope.
    exchange(runtime, 2, at(20, 10), '好的', '嗯。')
    assert not any(key.startswith('project_time_') for key in port.calls[-1]['questions'])
