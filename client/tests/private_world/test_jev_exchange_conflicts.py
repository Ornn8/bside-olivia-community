"""Updates that disagree about one item never cost the exchange its other facts.

A scripted decision port stands in for the service; nothing here reaches a provider.
"""
import asyncio
import hashlib

import pytest

from runtime.private_world import jev_exchange
from runtime.private_world.jev_exchange import extract

CONTRACT = 'CONTRACT-TEXT'
USER_SENTENCE = '小林，我通常晚上十一点半睡觉，我这边是UTC+8。'
RECORDED = '今天下午终于把《月光》第一乐章完整录下来了，录了三遍才满意。'
PLAN = '等我剪好就发给你听。'
BOUNDARY = '以后夜里十一点以后别给我打电话。'
MOONLIGHT = {'projects': [{'id': 'p8', 'title': '把《月光》第一乐章完整录下来', 'kind': 'linli', 'status': 'planned'}]}


def quote_ref(state, text):
    for ref, (start, end) in state['quotes'].items():
        source = state['sources']['user_letter' if ref.startswith('u') else 'linli_reply']
        if source[start:end] == text:
            return ref
    raise AssertionError(text)


# Every other fact an exchange can carry, answered the same way in every scenario below.
FACTS = {
    'world_update': 'reconsider',
    'current_quote': lambda state: quote_ref(state, RECORDED),
    'relationship': 'meaningful_exchange',
    'relationship_user_quote': lambda state: quote_ref(state, USER_SENTENCE),
    'relationship_reply_quote': lambda state: quote_ref(state, RECORDED),
    'routine': 'set',
    'routine_quote': lambda state: quote_ref(state, USER_SENTENCE),
    'sleep_hour': '23', 'sleep_minute': '30', 'utc_offset': '480', 'utc_offset_literal': 't0',
    'address_linli_quote': lambda state: quote_ref(state, '小林，'),
    'boundary_0_quote': lambda state: quote_ref(state, BOUNDARY),
}


class Port:
    """Answers from a script: one spec per update slot, fixed answers for the rest, and a
    ``conflict`` callable (or exception) for the extra request that decides a conflict."""

    def __init__(self, updates, *, conflict=None, retry=False, facts=FACTS):
        self.updates, self.conflict, self.retry, self.facts = updates, conflict, retry, facts
        self.calls = []

    async def ask(self, state, questions, *, purpose):
        self.calls.append((state, questions, purpose))
        if any(key.startswith('conflict_') for key in questions):
            if isinstance(self.conflict, BaseException):
                raise self.conflict
            return self.conflict(state, questions)
        answers = {key: 'none' if 'none' in q['criteria'] else next(iter(q['criteria'])) for key, q in questions.items()}
        if 'capacity' in answers:
            # The first request offers three slots; with ``retry`` it reports that they are not enough.
            answers['capacity'] = 'unsupported' if self.retry and 'update_3_quote' not in questions and len(
                [call for call in self.calls if 'capacity' in call[1]]) == 1 else 'ok'
        for i, spec in enumerate(self.updates):
            if f'update_{i}_quote' in questions:
                answers[f'update_{i}_quote'] = next(
                    ref for ref, anchor in state['action_anchors'].items() if anchor['text'] == spec['text'])
            if f'update_{i}_existing_match' in questions:
                answers[f'update_{i}_existing_match'] = spec.get('match', 'none')
                answers[f'update_{i}_applicability_kind'] = spec['kind']
                answers[f'update_{i}_status'] = spec['status']
                options = questions[f'update_{i}_evidence']['criteria']
                answers[f'update_{i}_evidence'] = next(
                    ref for ref, option in options.items() if ref != 'none' and option['text'] == spec['evidence'])
        for key, value in self.facts.items():
            if key in questions:
                answers[key] = value(state) if callable(value) else value
        return answers

    @property
    def purposes(self):
        return [purpose for _, _, purpose in self.calls]


def run(data, port, request_id='life:conflict', **kwargs):
    events = []
    payload = asyncio.run(extract(port, data, CONTRACT, request_id, diagnostic_observer=events.append, **kwargs))
    return payload, events


def counters(events):
    return [event for event in events if event['phase'] == 'exchange-conflict']


def rest(payload):
    return {key: value for key, value in payload.items() if key != 'updates'}


def new_id(request_id, kind, anchor_text, support=None):
    return 'jev.' + hashlib.sha256(
        (request_id + '\0' + kind + '\0' + (support or anchor_text) + '\0' + anchor_text).encode()).hexdigest()[:40]


MOON_DATA = {'user_letter': USER_SENTENCE + '你今天过得怎么样？', 'previous_state': MOONLIGHT,
             'linli_reply': RECORDED + PLAN + BOUNDARY}
RECORD = {'text': '今天下午终于把《月光》第一乐章完整录下来了，', 'match': 'p0', 'kind': 'linli',
          'status': 'completed', 'evidence': RECORDED}
PROMISE = {'text': PLAN, 'match': 'p0', 'kind': 'shared', 'status': 'planned', 'evidence': PLAN}


def moonlight(conflict=None):
    return Port([RECORD, PROMISE], conflict=conflict)


def test_every_other_fact_is_produced_when_no_updates_conflict():
    # The control for the scenarios below: the same exchange with one plain update.
    payload, _ = run(MOON_DATA, Port([RECORD]))
    assert [(item['id'], item['status']) for item in payload['updates']] == [('p8', 'completed')]
    assert payload['relationship']['kind'] == 'meaningful_exchange'
    assert payload['routine'] == {'sleep_minute': 1410, 'utc_offset_minutes': 480, 'quote': USER_SENTENCE}
    assert payload['boundaries'] == [{'action': 'set', 'boundary_id': None, 'quote': BOUNDARY}]
    assert payload['addressing'] == {'user_calls_linli': '小林，'}
    assert payload['current_quote'] == RECORDED and payload['world_update'] == 'reconsider'


def test_kind_mismatch_detaches_the_update_into_a_new_item_of_its_own_kind():
    port = moonlight()
    payload, events = run(MOON_DATA, port)
    control, _ = run(MOON_DATA, Port([RECORD]))
    old, new = payload['updates']
    assert (old['id'], old['title'], old['status'], old['kind'], old['quote']) == (
        'p8', '把《月光》第一乐章完整录下来', 'completed', 'linli', RECORDED)
    assert new == {'id': new_id('life:conflict', 'shared', PLAN), 'title': PLAN, 'detail': PLAN, 'status': 'planned',
                   'kind': 'shared', 'actor': 'linli', 'quote': PLAN}
    assert rest(payload) == rest(control) and payload['relationship'] and payload['routine'] and payload['boundaries']
    # No extra request: the kind alone decides, and the result is two exchange requests.
    assert port.purposes == ['exchange-facts', 'exchange-anchored-facts']
    assert counters(events) == [{'phase': 'exchange-conflict', 'outcome': 'detached', 'count': 1}]
    assert [event['phase'] for event in events] == ['exchange-facts', 'exchange-anchored-facts', 'exchange-conflict']


def test_a_detached_update_is_identified_like_any_new_item_of_that_kind():
    detached, _ = run(MOON_DATA, moonlight(), 'life:moon')
    plain, _ = run(MOON_DATA, Port([RECORD, {**PROMISE, 'match': 'none'}]), 'life:moon')
    again, _ = run(MOON_DATA, moonlight(), 'life:moon:correct')
    assert detached['updates'][1] == plain['updates'][1]
    assert [item['id'] for item in detached['updates']] == [item['id'] for item in again['updates']]
    assert detached['updates'][1]['id'] == new_id('life:moon', 'shared', PLAN)


def test_every_conflicting_update_of_another_kind_detaches():
    both = {**RECORD, 'kind': 'shared'}
    payload, events = run(MOON_DATA, Port([both, PROMISE]))
    assert [(item['kind'], item['status']) for item in payload['updates']] == [('shared', 'completed'), ('shared', 'planned')]
    assert 'p8' not in {item['id'] for item in payload['updates']}
    assert len({item['id'] for item in payload['updates']}) == 2
    assert counters(events) == [{'phase': 'exchange-conflict', 'outcome': 'detached', 'count': 2}]


def test_a_detached_update_is_still_subject_to_the_rules_of_its_own_kind():
    data = {**MOON_DATA, 'origin': 'proactive'}
    payload, events = run(data, moonlight())
    # A proactive letter records no shared plan, so only the plain update remains.
    assert [(item['id'], item['status']) for item in payload['updates']] == [('p8', 'completed')]
    assert counters(events) == [{'phase': 'exchange-conflict', 'outcome': 'detached', 'count': 1}]


def test_detaching_one_update_does_not_swallow_another_update_that_agrees_with_it():
    # The promise (shared, planned) leaves the item; the remaining same-kind updates still disagree
    # and are decided, instead of the plain plan having been merged into the promise first.
    plain_plan = {'text': '录了三遍才满意。', 'match': 'p0', 'kind': 'linli', 'status': 'planned', 'evidence': RECORDED}
    port = Port([RECORD, PROMISE, plain_plan], conflict=decide('completed'))
    payload, events = run(MOON_DATA, port)
    assert [(item['id'], item['kind'], item['status']) for item in payload['updates']][0] == ('p8', 'linli', 'completed')
    assert [(item['kind'], item['status'], item['title']) for item in payload['updates']][1:] == [('shared', 'planned', PLAN)]
    assert list(port.calls[2][1]['conflict_0']['criteria']) == ['completed', 'planned', 'unclear']
    assert len(port.calls) == 3
    assert counters(events) == [{'phase': 'exchange-conflict', 'outcome': 'detached', 'count': 1},
                                {'phase': 'exchange-conflict', 'outcome': 'decided', 'count': 1}]


def test_a_lone_kind_disagreement_is_not_a_conflict_and_keeps_the_item_kind():
    port = Port([PROMISE])
    payload, events = run(MOON_DATA, port)
    assert [(item['id'], item['kind'], item['status']) for item in payload['updates']] == [('p8', 'linli', 'planned')]
    assert counters(events) == [] and port.purposes == ['exchange-facts', 'exchange-anchored-facts']


PRACTICE = {'projects': [{'id': 'practice', 'title': '练曲', 'kind': 'linli', 'status': 'planned'}]}
DONE, STILL = '我练完这首曲子了。', '我现在在练这首曲子。'
PRACTICE_DATA = {'user_letter': USER_SENTENCE, 'linli_reply': DONE + STILL + RECORDED + BOUNDARY, 'previous_state': PRACTICE}
SPECS = [{'text': DONE, 'match': 'p0', 'kind': 'linli', 'status': 'completed', 'evidence': DONE},
         {'text': STILL, 'match': 'p0', 'kind': 'linli', 'status': 'ongoing', 'evidence': STILL}]


def decide(choice):
    return lambda state, questions: {key: choice for key in questions}


def test_a_same_kind_conflict_is_decided_by_one_extra_request_and_its_answer_applies():
    port = Port(SPECS, conflict=decide('ongoing'))
    payload, events = run(PRACTICE_DATA, port)
    control, _ = run(PRACTICE_DATA, Port(SPECS[:1]))
    assert [(item['id'], item['status'], item['quote']) for item in payload['updates']] == [('practice', 'ongoing', STILL)]
    assert rest(payload) == rest(control) and payload['relationship'] and payload['routine']
    assert port.purposes == ['exchange-facts', 'exchange-anchored-facts', 'exchange-anchored-facts']
    state, questions, _ = port.calls[2]
    assert set(state) == {'contract', 'conflicts', 'quote_contract'}
    assert state['contract'] == CONTRACT and state['quote_contract'] == jev_exchange._QUOTE_CONTRACT
    assert state['conflicts'] == {'c0': {
        'item': {'title': '练曲', 'kind': 'linli', 'status': 'planned'},
        'quotes': [{'source': 'linli_reply', 'text': DONE}, {'source': 'linli_reply', 'text': STILL}]}}
    assert list(questions) == ['conflict_0']
    question = questions['conflict_0']
    assert list(question['criteria']) == ['completed', 'ongoing', 'unclear']
    assert DONE in question['criteria']['completed'] and STILL in question['criteria']['ongoing']
    assert '最终状态' in question['instructions'] and '用户本人的更正或取消优先' in question['instructions']
    assert [event['phase'] for event in events] == [
        'exchange-facts', 'exchange-anchored-facts', 'exchange-anchored-facts', 'exchange-conflict']
    assert set(events[2]['decisions']) == {'conflict_0'}
    assert counters(events) == [{'phase': 'exchange-conflict', 'outcome': 'decided', 'count': 1}]


def test_the_extra_request_names_the_source_of_each_quote():
    from_user = {'text': '录音今天不用发了。', 'match': 'p0', 'kind': 'shared', 'status': 'completed',
                   'evidence': '录音今天不用发了。'}
    data = {'user_letter': '录音今天不用发了。', 'linli_reply': '我今天处理那份录音。',
            'previous_state': {'shared': [{'id': 'send', 'title': '发录音', 'kind': 'shared', 'status': 'planned'}]}}
    reply = {'text': '我今天处理那份录音。', 'match': 'p0', 'kind': 'shared', 'status': 'ongoing',
             'evidence': '我今天处理那份录音。'}
    port = Port([from_user, reply], conflict=decide('unclear'), facts={})
    run(data, port)
    quotes = port.calls[2][0]['conflicts']['c0']['quotes']
    assert quotes == [{'source': 'user_letter', 'text': '录音今天不用发了。'},
                      {'source': 'linli_reply', 'text': '我今天处理那份录音。'}]
    assert port.calls[2][0]['conflicts']['c0']['item'] == {'title': '发录音', 'kind': 'shared', 'status': 'planned'}


def test_unclear_leaves_the_item_unchanged_and_keeps_the_rest_of_the_payload():
    port = Port(SPECS, conflict=decide('unclear'))
    payload, events = run(PRACTICE_DATA, port)
    control, _ = run(PRACTICE_DATA, Port(SPECS[:1]))
    assert payload['updates'] == []
    assert rest(payload) == rest(control) and payload['relationship'] and payload['routine'] and payload['boundaries']
    assert len(port.calls) == 3
    assert counters(events) == [{'phase': 'exchange-conflict', 'outcome': 'unresolved', 'count': 1}]


@pytest.mark.parametrize('failure', [
    ValueError('JEV_UNAVAILABLE'), ValueError('JEV_INPUT_TOO_LARGE'), RuntimeError('boom'), TimeoutError(),
], ids=lambda failure: type(failure).__name__ + str(failure))
def test_a_failing_extra_request_leaves_the_item_unchanged_and_is_not_retried(failure):
    port = Port(SPECS, conflict=failure)
    payload, events = run(PRACTICE_DATA, port)
    control, _ = run(PRACTICE_DATA, Port(SPECS[:1]))
    assert payload['updates'] == [] and rest(payload) == rest(control)
    # The exchange is neither failed nor repeated at another quote level.
    assert port.purposes == ['exchange-facts', 'exchange-anchored-facts', 'exchange-anchored-facts']
    assert counters(events) == [{'phase': 'exchange-conflict', 'outcome': 'unresolved', 'count': 1}]


@pytest.mark.parametrize('answer', [{'conflict_0': 'ongoing-ish'}, {}, {'conflict_0': 'ongoing', 'extra': 'none'}])
def test_an_invalid_extra_answer_is_a_failure_of_the_extra_request(answer):
    port = Port(SPECS, conflict=lambda state, questions: answer)
    payload, events = run(PRACTICE_DATA, port)
    assert payload['updates'] == [] and payload['relationship']
    assert counters(events) == [{'phase': 'exchange-conflict', 'outcome': 'unresolved', 'count': 1}]


def test_three_updates_of_one_item_are_one_question_with_each_status():
    specs = [*SPECS, {'text': RECORDED, 'match': 'p0', 'kind': 'linli', 'status': 'planned', 'evidence': RECORDED}]
    port = Port(specs, conflict=decide('planned'))
    payload, _ = run(PRACTICE_DATA, port)
    assert [(item['id'], item['status']) for item in payload['updates']] == [('practice', 'planned')]
    assert list(port.calls[2][1]['conflict_0']['criteria']) == ['completed', 'ongoing', 'planned', 'unclear']
    assert len(port.calls[2][0]['conflicts']['c0']['quotes']) == 3


WALK, PHOTOS = '我去散步了。', '我把照片寄出去了。'
TWO_ITEMS = {'projects': [PRACTICE['projects'][0],
                          {'id': 'photos', 'title': '寄照片', 'kind': 'linli', 'status': 'planned'}]}
MORE = [{'text': PHOTOS, 'match': 'p1', 'kind': 'linli', 'status': 'completed', 'evidence': PHOTOS},
        {'text': '我打算周末再寄一次照片。', 'match': 'p1', 'kind': 'linli', 'status': 'planned',
         'evidence': '我打算周末再寄一次照片。'}]
SEVERAL = {'user_letter': USER_SENTENCE, 'previous_state': TWO_ITEMS,
           'linli_reply': DONE + STILL + PHOTOS + '我打算周末再寄一次照片。' + WALK + RECORDED + BOUNDARY}
WALKING = {'text': WALK, 'kind': 'linli', 'status': 'completed', 'evidence': WALK}


def test_several_conflicting_items_are_decided_in_a_single_extra_request():
    answers = {'conflict_0': 'completed', 'conflict_1': 'planned'}
    port = Port([*SPECS, *MORE, WALKING], conflict=lambda state, questions: answers, retry=True)
    payload, events = run(SEVERAL, port)
    assert port.purposes == ['exchange-facts', 'exchange-facts', 'exchange-anchored-facts', 'exchange-anchored-facts']
    state, questions, _ = port.calls[3]
    assert list(questions) == ['conflict_0', 'conflict_1'] and list(state['conflicts']) == ['c0', 'c1']
    assert [state['conflicts'][key]['item']['title'] for key in ('c0', 'c1')] == ['练曲', '寄照片']
    # The undisputed update is not part of the question and keeps its place between the two items.
    assert [(item['id'], item['status']) for item in payload['updates']][:2] == [('practice', 'completed'), ('photos', 'planned')]
    assert payload['updates'][2]['status'] == 'completed' and payload['updates'][2]['title'] == WALK[:60]
    assert len(payload['updates']) == 3 and payload['relationship'] and payload['routine']
    assert counters(events) == [{'phase': 'exchange-conflict', 'outcome': 'decided', 'count': 2}]


def test_decided_and_unresolved_items_are_reported_separately_in_one_request():
    answers = {'conflict_0': 'unclear', 'conflict_1': 'completed'}
    port = Port([*SPECS, *MORE, WALKING], conflict=lambda state, questions: answers, retry=True)
    payload, events = run(SEVERAL, port)
    assert port.purposes.count('exchange-anchored-facts') == 2
    assert [(item['id'], item['status']) for item in payload['updates']][:1] == [('photos', 'completed')]
    assert 'practice' not in {item['id'] for item in payload['updates']} and len(payload['updates']) == 2
    assert counters(events) == [{'phase': 'exchange-conflict', 'outcome': 'decided', 'count': 1},
                                {'phase': 'exchange-conflict', 'outcome': 'unresolved', 'count': 1}]


def test_no_extra_request_and_no_counter_without_a_conflict():
    port = Port([SPECS[0], {'text': RECORDED, 'kind': 'linli', 'status': 'completed', 'evidence': RECORDED}])
    payload, events = run(PRACTICE_DATA, port)
    assert len(payload['updates']) == 2
    assert port.purposes == ['exchange-facts', 'exchange-anchored-facts']
    assert counters(events) == [] and [event['phase'] for event in events] == ['exchange-facts', 'exchange-anchored-facts']
    quiet, _ = run(PRACTICE_DATA, Port([SPECS[0]]))
    assert quiet['updates'] == payload['updates'][:1]


def test_user_cancellation_and_equal_statuses_still_settle_without_a_question():
    data = {'user_letter': '录音今天不用发了。', 'linli_reply': '我今天会处理那份录音。',
            'previous_state': {'shared': [{'id': 'send', 'title': '发录音', 'kind': 'shared'}]}}
    for reply_status in ('cancelled', 'planned'):
        port = Port([{'text': '录音今天不用发了。', 'match': 'p0', 'kind': 'shared', 'status': 'cancelled',
                      'evidence': '录音今天不用发了。'},
                     {'text': '我今天会处理那份录音。', 'match': 'p0', 'kind': 'shared', 'status': reply_status,
                      'evidence': '我今天会处理那份录音。'}], facts={})
        payload, events = run(data, port)
        assert [(item['status'], item['actor']) for item in payload['updates']] == [('cancelled', 'user')]
        assert port.purposes == ['exchange-facts', 'exchange-anchored-facts'] and counters(events) == []


def test_the_observer_cannot_change_what_a_conflict_decides():
    port = Port(SPECS, conflict=decide('ongoing'))

    def meddle(event):
        event.clear()

    payload = asyncio.run(extract(port, PRACTICE_DATA, CONTRACT, 'life:meddle', diagnostic_observer=meddle))
    assert [item['status'] for item in payload['updates']] == ['ongoing']


def test_conflicts_are_decided_without_an_observer_too():
    payload = asyncio.run(extract(Port(SPECS, conflict=decide('completed')), PRACTICE_DATA, CONTRACT, 'life:quiet'))
    assert [item['status'] for item in payload['updates']] == ['completed']


# The extra request obeys the exchange bound: items that do not fit are left unchanged.
def _conflicting(count):
    candidates = []
    for n in range(count):
        old = {'id': f'item{n}', 'title': f'事项{n}', 'kind': 'linli', 'status': 'planned'}
        for status in ('completed', 'ongoing'):
            text = f'第{n}项的{status}说法，' + '很长的原文' * 20
            candidates.append({'anchor': {'source': 'linli_reply', 'text': text, 'support': text, 'span': [0, len(text)]},
                               'quote': text, 'status': status, 'applicability': 'linli', 'old': old})
    slots, detached = jev_exchange._assemble_updates(candidates, 'life:size', False)
    assert detached == 0 and len(slots) == count and all(len(entries) == 2 for _, entries in slots)
    return slots


class Recorder:
    def __init__(self, choice='completed'):
        self.calls, self.choice = [], choice

    async def ask(self, state, questions, *, purpose):
        self.calls.append((state, questions, purpose))
        return {key: self.choice for key in questions}


def test_items_that_do_not_fit_the_bound_are_left_unchanged(monkeypatch):
    slots = _conflicting(3)
    full = Recorder()
    events = []
    asyncio.run(jev_exchange._resolve_updates(full, CONTRACT, slots, 0, diagnostic_observer=events.append))
    (state, questions, purpose), = full.calls
    assert len(questions) == 3
    size = jev_exchange._request_bytes(state, questions, purpose)
    monkeypatch.setattr(jev_exchange, 'EXCHANGE_MAX_INPUT_BYTES', size - 1)
    trimmed, events = Recorder(), []
    updates = asyncio.run(jev_exchange._resolve_updates(trimmed, CONTRACT, slots, 0, diagnostic_observer=events.append))
    (state, questions, purpose), = trimmed.calls
    assert list(questions) == ['conflict_0', 'conflict_1'] and jev_exchange._request_bytes(state, questions, purpose) < size
    assert [item['id'] for item in updates] == ['item0', 'item1']
    assert [(event['outcome'], event['count']) for event in counters(events)] == [('decided', 2), ('unresolved', 1)]
    monkeypatch.setattr(jev_exchange, 'EXCHANGE_MAX_INPUT_BYTES', 100)
    none_fit, events = Recorder(), []
    updates = asyncio.run(jev_exchange._resolve_updates(none_fit, CONTRACT, slots, 0, diagnostic_observer=events.append))
    assert none_fit.calls == [] and updates == []
    assert [(event['outcome'], event['count']) for event in counters(events)] == [('unresolved', 3)]
