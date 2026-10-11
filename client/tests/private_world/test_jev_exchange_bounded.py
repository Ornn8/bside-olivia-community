"""The two exchange decision requests stay small however much history a user has.

Production: ~15% of exchange-facts and ~20-25% of exchange-anchored-facts requests were
rejected by the decision service (max_tokens_exceeded, smallest rejection 70.7 KB), so those
exchanges recorded no facts. Everything here is synthetic: a scripted port records the
serialized request sizes; no provider is called.
"""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from runtime.private_world import jev_exchange
from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import _EXCHANGE_LIFE_PROMPT
from runtime.private_world.jev_exchange import extract
from tests.private_world import long_exchange as fx

LIMIT = 45_000


def wire_size(state, questions, purpose):
    return len(json.dumps(dict(state=state, questions=questions, purpose=purpose), ensure_ascii=False,
                          separators=(',', ':'), sort_keys=True, allow_nan=False).encode())


def sentences(text):
    return [m.group().strip() for m in re.finditer(r'[^。！？!?\n]+[。！？!?]?', text) if m.group().strip()]


class ScriptedPort:
    """Answers like a service that finds one action in the letter and two in the reply."""

    def __init__(self, data, *, slots=3, match=None, routine=False, boundary=False):
        self.data, self.slots, self.match, self.routine, self.boundary = data, slots, match or {}, routine, boundary
        self.calls, self.sizes = [], []
        user, reply = sentences(data['user_letter']), sentences(data['linli_reply'])
        pick = lambda items, index: items[min(index, len(items) - 1)]  # noqa: E731 - short texts have few sentences
        self.picks = [('u', pick(user, 3)), ('r', pick(reply, 2)), ('r', pick(reply, 6))][:slots]
        self.boundary_pick = pick(reply, 4)

    def ref_for(self, state, prefix, sentence):
        source = self.data['user_letter' if prefix == 'u' else 'linli_reply']
        start = source.find(sentence)
        return next(ref for ref, anchor in state['action_anchors'].items()
                    if ref.startswith(prefix) and anchor['span'] == [start, start + len(sentence)])

    async def ask(self, state, questions, *, purpose):
        self.calls.append((state, questions, purpose))
        self.sizes.append((purpose, wire_size(state, questions, purpose)))
        answers = {key: 'none' if 'none' in q['criteria'] else next(iter(q['criteria'])) for key, q in questions.items()}
        if 'capacity' in answers:
            answers['capacity'] = 'ok'
        for i, (prefix, sentence) in enumerate(self.picks):
            if f'update_{i}_quote' in questions:
                answers[f'update_{i}_quote'] = self.ref_for(self.calls[0][0], prefix, sentence)
            if f'update_{i}_existing_match' in questions:
                answers[f'update_{i}_existing_match'] = self.match.get(i, 'none')
                answers[f'update_{i}_applicability_kind'] = 'shared' if prefix == 'u' else 'linli'
                answers[f'update_{i}_status'] = 'planned'
                options = questions[f'update_{i}_evidence']['criteria']
                answers[f'update_{i}_evidence'] = next(
                    (ref for ref, option in options.items() if ref != 'none' and option['text'] == sentence),
                    next(ref for ref in options if ref != 'none'))
        if self.boundary and 'boundary_0_quote' in answers:
            answers['boundary_0_quote'] = self.ref_for(self.calls[0][0], 'r', self.boundary_pick)
        if self.routine and 'routine' in answers:
            answers['routine'] = 'set'
            answers['routine_quote'] = next(ref for ref, (start, end) in state['quotes'].items()
                                            if ref.startswith('u') and 'UTC+8' in self.data['user_letter'][start:end])
        for key, value in {'sleep_hour': '23', 'sleep_minute': '30', 'utc_offset': '480'}.items():
            if key in answers:
                answers[key] = value
        return answers

    def sizes_of(self, purpose):
        return [size for name, size in self.sizes if name == purpose]


def run(data, **kwargs):
    port = ScriptedPort(data, **kwargs)
    return asyncio.run(extract(port, data, _EXCHANGE_LIFE_PROMPT, 'life:bounded')), port


@pytest.mark.parametrize('letter,reply', [(1500, 1500), (2000, 1500), (2400, 2000)])
@pytest.mark.parametrize('slots', [1, 3])
def test_long_history_and_long_texts_keep_both_requests_under_45kb(letter, reply, slots):
    data = fx.build(letter_chars=letter, reply_chars=reply)
    assert sum(len(v) for v in data['previous_state'].values()) >= 150
    assert sum(item['status'] not in ('completed', 'cancelled')
               for v in data['previous_state'].values() for item in v) >= 80
    assert 1500 <= len(data['user_letter']) <= 2500 and data['user_letter'].count('，') > len(data['user_letter']) / 20
    payload, port = run(data, slots=slots)
    assert {name for name, _ in port.sizes} == {'exchange-facts', 'exchange-anchored-facts'}
    assert max(size for _, size in port.sizes) <= LIMIT, port.sizes
    assert LIMIT <= 46_080 and jev_exchange.EXCHANGE_MAX_INPUT_BYTES <= LIMIT
    # Evidence stays verbatim: every recorded quote is an exact span of its own source.
    assert payload['updates']
    for item in payload['updates']:
        source = data['user_letter' if item['actor'] == 'user' else 'linli_reply']
        assert item['quote'] in source and item['detail'] in source


def test_request_size_does_not_grow_with_project_history():
    sizes = {}
    for count in (30, 160, 600):
        data = fx.build(letter_chars=1500, reply_chars=1500, project_count=count, open_count=count * 9 // 16)
        _, port = run(data)
        sizes[count] = (port.sizes_of('exchange-facts'), port.sizes_of('exchange-anchored-facts'))
    for purpose in (0, 1):
        assert abs(sizes[160][purpose][0] - sizes[600][purpose][0]) < 1_500, sizes
        assert max(size[purpose][0] for size in sizes.values()) <= LIMIT


def test_each_request_is_smaller_than_the_unbounded_history_it_replaces():
    data = fx.build(letter_chars=650, reply_chars=700)
    before = json.dumps(data['previous_state'], ensure_ascii=False).encode()
    _, port = run(data)
    shown = port.calls[0][0]['exchange']['previous_state']
    assert sum(len(v) for v in shown.values()) == jev_exchange.MAX_KNOWN_ITEMS
    assert len(json.dumps(shown, ensure_ascii=False).encode()) < len(before) / 8


def test_stale_open_items_appear_only_when_this_exchange_mentions_them():
    data = fx.build(letter_chars=1500, reply_chars=1500)
    texts = data['user_letter'] + '\n' + data['linli_reply']
    shown = jev_exchange._bounded_previous(data['previous_state'], texts)
    items = shown['projects'] + shown['shared']
    ids = {item['id'] for item in items}
    stale_mentioned = ['jev.' + __import__('hashlib').sha256(f'fixture{n}'.encode()).hexdigest()[:40] for n in range(3)]
    assert all(item_id in ids for item_id in stale_mentioned)
    assert len(items) == jev_exchange.MAX_KNOWN_ITEMS
    # Everything else shown is the newest open work, newest first within each group.
    others = [item for item in items if item['id'] not in stale_mentioned]
    assert all(item['status'] not in ('completed', 'cancelled') for item in others[:10])
    for group in shown.values():
        stamps = [item['updated_at'] for item in group]
        assert stamps == sorted(stamps, reverse=True)
    newest_open = sorted((item for v in data['previous_state'].values() for item in v
                          if item['status'] not in ('completed', 'cancelled') and item['id'] not in stale_mentioned),
                         key=lambda item: item['updated_at'], reverse=True)
    fresh = [item for item in others if item['id'] in {n['id'] for n in newest_open[:5]}]
    assert len(fresh) == 5


def test_history_that_already_fits_is_sent_unchanged_and_in_order():
    previous = {'projects': [{'id': f'p{i}', 'title': f'练习{i}', 'kind': 'linli', 'status': 'ongoing',
                              'updated_at': f'2026-10-0{i + 1}T00:00:00+00:00'} for i in range(5)],
                'shared': [{'id': 's0', 'title': '寄书', 'kind': 'shared', 'status': 'planned'}]}
    assert jev_exchange._bounded_previous(previous, '随便聊聊') is previous
    boundaries = [{'boundary_id': f'b{i}', 'scope': f'规则{i}'} for i in range(jev_exchange.MAX_KNOWN_BOUNDARIES)]
    assert jev_exchange._bounded_boundaries(boundaries, '随便聊聊') == boundaries


def test_a_choice_maps_back_to_the_durable_id_of_the_item_it_names():
    data = fx.build(letter_chars=650, reply_chars=700)
    wanted = data['previous_state']['projects'][0]  # stale, open and mentioned by this exchange
    port = ScriptedPort(data, slots=2)
    original = port.ask

    async def match_by_title(state, questions, *, purpose):
        if purpose == 'exchange-anchored-facts':
            directory = state['exchange']['previous_state']
            port.match = {1: next(key for key, entry in directory.items() if entry['title'] == wanted['title'])}
        return await original(state, questions, purpose=purpose)

    port.ask = match_by_title
    payload = asyncio.run(extract(port, data, _EXCHANGE_LIFE_PROMPT, 'life:map'))
    assert wanted['id'] in {item['id'] for item in payload['updates']}
    directory = port.calls[1][0]['exchange']['previous_state']
    assert all(set(entry) <= {'title', 'actor', 'kind', 'status'} for entry in directory.values())
    # The durable ids never travel: the service names an item by its directory key.
    assert all('id' not in entry for group in port.calls[0][0]['exchange']['previous_state'].values() for entry in group)
    assert not any(item['id'] in json.dumps(state, ensure_ascii=False) for state, _, _ in port.calls
                   for item in data['previous_state']['projects'] + data['previous_state']['shared'])


def test_anchor_wire_has_no_repeated_support_and_support_refs_resolve():
    data = fx.build(letter_chars=300, reply_chars=300, project_count=0, open_count=0, boundary_count=0)
    _, port = run(data, slots=1)
    state = port.calls[0][0]
    anchors = state['action_anchors']
    assert all(set(anchor) <= {'text', 'span', 'support'} for anchor in anchors.values())
    resolved = {ref: anchors[anchor['support']]['text'] if 'support' in anchor else anchor['text']
                for ref, anchor in anchors.items()}
    for ref, anchor in anchors.items():
        source = data['user_letter' if ref.startswith('u') else 'linli_reply']
        assert source[slice(*anchor['span'])] == anchor['text']
        assert resolved[ref] in source and anchor['text'] in resolved[ref]
        assert len(resolved[ref]) <= 240
    # A supporting sentence is written once, in its own anchor, however many anchors rely on it.
    supports = {anchor['support'] for anchor in anchors.values() if 'support' in anchor}
    assert supports and all(not anchors[ref].get('support') for ref in supports)  # a support is its own anchor
    assert len(json.dumps(anchors, ensure_ascii=False).encode()) < len(json.dumps(
        jev_exchange._action_anchors(jev_exchange._quotes(data['user_letter'], 'u', 0), {
            'user_letter': data['user_letter'], 'linli_reply': ''}), ensure_ascii=False).encode()) / 2


def test_coarsest_level_shortens_only_the_displayed_anchor_text():
    reply = '我明天一早去图书馆还书，顺便把借来的两本植物图鉴也一起还了，然后回家把阳台上的花全部浇一遍。'
    quotes = jev_exchange._quotes(reply, 'r', 2)
    anchors = jev_exchange._action_anchors(quotes, {'user_letter': '', 'linli_reply': reply}, with_atoms=False)
    wire = jev_exchange._anchor_wire(anchors, abbreviate=True)
    ref, = wire
    assert '…' in wire[ref]['text'] and len(wire[ref]['text']) < len(reply)
    assert reply[slice(*wire[ref]['span'])] == reply == anchors[ref]['text']


def test_dependent_questions_split_into_fitting_requests_instead_of_failing(monkeypatch):
    data = fx.build(letter_chars=150, reply_chars=200, project_count=30, open_count=17, boundary_count=6)
    data['user_letter'] += '我通常晚上十一点半睡觉，我这边是UTC+8。'
    whole, together = run(data, slots=3, routine=True, boundary=True)
    facts, = together.sizes_of('exchange-facts')
    dependent, = together.sizes_of('exchange-anchored-facts')
    assert dependent > facts + 300  # the scenario: one dependent request larger than the facts request
    # The same exchange under a bound that only the facts request just fits: the independent
    # question groups travel in several requests, each within the bound, with the same result.
    monkeypatch.setattr(jev_exchange, 'EXCHANGE_MAX_INPUT_BYTES', facts + 100)
    split, port = run(data, slots=3, routine=True, boundary=True)
    sizes = port.sizes_of('exchange-anchored-facts')
    assert len(sizes) >= 2 and max(port.sizes, key=lambda item: item[1])[1] <= facts + 100
    assert split == whole and split['routine']['utc_offset_minutes'] == 480 and len(split['updates']) >= 2
    questions = [set(q) for _, q, name in port.calls if name == 'exchange-anchored-facts']
    assert set().union(*questions) == set(together.calls[1][1]) and sum(map(len, questions)) == len(together.calls[1][1])


def test_common_exchange_still_uses_two_requests():
    for letter, reply in ((150, 200), (650, 700), (1000, 1000)):
        data = fx.build(letter_chars=letter, reply_chars=reply, project_count=30, open_count=17, boundary_count=6)
        _, port = run(data)
        assert [name for name, _ in port.sizes] == ['exchange-facts', 'exchange-anchored-facts']


def test_text_too_long_for_any_level_fails_before_any_paid_call():
    data = fx.build(letter_chars=6000, reply_chars=6000)
    port = ScriptedPort(data)
    with pytest.raises(ValueError, match='^JEV_INPUT_TOO_LARGE$'):
        asyncio.run(extract(port, data, _EXCHANGE_LIFE_PROMPT, 'life:too-long'))
    assert port.calls == []


def test_utc_offset_options_keep_their_keys_with_short_labels():
    data = fx.build(letter_chars=300, reply_chars=300, project_count=0, open_count=0, boundary_count=0)
    data['user_letter'] += '我通常晚上十一点半睡觉，我这边是UTC+8。'
    _, port = run(data, slots=1, routine=True)
    options = next(q for _, questions, _ in port.calls for key, q in questions.items() if key == 'utc_offset')['criteria']
    assert set(options) == {'unknown', *map(str, range(-720, 841, 15))}
    assert options['480'] == 'UTC+08:00' and options['-210'] == 'UTC-03:30' and options['345'] == 'UTC+05:45'
    assert len(json.dumps(options, ensure_ascii=False).encode()) < 3_000


def test_conduct_request_is_bounded_by_boundaries_too():
    # Boundary scopes may be up to 200 characters; a hundred of them are ~60 KB on their own.
    boundaries = [{**item, 'scope': (item['scope'] * 6)[:190]} for item in fx.boundaries(100)]
    data = {'user_letter': fx.text(3, 1500), 'active_boundaries': boundaries}
    sizes = []

    class Port:
        async def ask(self, state, questions, *, purpose):
            sizes.append(wire_size(state, questions, purpose))
            return {'conduct': 'none', 'quote': 'none'}

    asyncio.run(jev_exchange.conduct(Port(), data, '', 'life:c:conduct', conflict=True))
    assert sizes and max(sizes) <= LIMIT


def test_full_slot_retry_may_be_larger_but_stays_far_below_the_service_ceiling():
    data = fx.build(letter_chars=650, reply_chars=700, project_count=30, open_count=17, boundary_count=6)

    class Retry(ScriptedPort):
        async def ask(self, state, questions, *, purpose):
            answers = await super().ask(state, questions, purpose=purpose)
            if 'capacity' in answers and len(self.calls) == 1:
                answers['capacity'] = 'unsupported'
            return answers

    port = Retry(data, slots=2)
    asyncio.run(extract(port, data, _EXCHANGE_LIFE_PROMPT, 'life:retry'))
    facts = port.sizes_of('exchange-facts')
    assert len(facts) == 2 and facts[0] <= LIMIT and facts[1] <= LIMIT * 1.25 < 60_000


def test_a_real_store_with_a_long_history_produces_a_small_request(tmp_path):
    """History comes from the real store (exchange_state), not from a hand-made dictionary."""
    now = datetime(2026, 10, 11, 4, tzinfo=timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    source = fx.build(project_count=160, open_count=90)
    for index, item in enumerate(source['previous_state']['projects'] + source['previous_state']['shared']):
        reply = sentences(fx.text(1000 + index, 120))[0]
        store.record_exchange(f'reply:history{index}', '好。', reply, [dict(
            id=item['id'], title=item['title'], detail=reply, status=item['status'], kind=item['kind'],
            actor='linli', quote=reply)], occurred_at=now - timedelta(days=min(index, 100), hours=index % 7))
    data = fx.build(letter_chars=1800, reply_chars=1500)
    data['previous_state'] = store.exchange_state(data['user_letter'], related_text=data['linli_reply'], now=now)
    assert sum(len(v) for v in data['previous_state'].values()) >= 80  # every open item, as before
    payload, port = run(data)
    assert max(size for _, size in port.sizes) <= LIMIT
    assert payload['updates']
