"""Whose words are whose: the writer reads each side under its own name, facts keep who calls whom, an entry told from
his side is not kept, and the user can have a day written again (2026-10-21)."""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from original_client_relay_api import RELAY_BASE
from runtime import model_routes
from runtime.diary.diary import (DiaryStore, SHANGHAI, day_messages, directed, due_days, for_writer, told_by_her,
                                 validate, write_day)

NOW = datetime(2026, 10, 8, 4, 0, tzinfo=SHANGHAI)
DAY = datetime(2026, 10, 7, tzinfo=SHANGHAI)


def letter(content, reply, hour):
    at = DAY + timedelta(hours=hour)
    return {'letter_id': 'l' + at.isoformat(), 'content': content, 'reply_text': reply, 'letter_status': 'COMPLETED',
            'life_received_at': at.isoformat()}


ROWS = [letter('今天练琴练到忘了吃饭，你别笑我', '小熊，那你下次定个闹钟', 12),
        letter('我喜欢吃辣，越辣越好', '我家林小颖今天又睡了一下午', 20)]
BODY = ('他今天练琴练到忘了吃饭，还叫我别笑他。我没笑，只是让他下次定个闹钟。晚上我跟他说林小颖又睡了一下午，'
        '他大概也想象得出那团毛蜷在沙发上的样子。写到这里我自己也有点困了。')
ENTRY = {'title': '闹钟', 'mood': '心软', 'body': BODY, 'facts': []}


class Gateway:
    """Answers the draft and the edit with the entry, and the narrator check with the given reading."""

    def __init__(self, entry, narrator='linli'):
        self.entry, self.narrator, self.calls = entry, narrator, []
        self.config = SimpleNamespace(model='m', base_url=RELAY_BASE)

    async def complete_structured_scoped(self, messages, **kwargs):
        self.calls.append((messages, {**kwargs, 'model': model_routes.routed_model(RELAY_BASE, 'm')}))
        if '-check:' in kwargs['request_id']:
            if isinstance(self.narrator, Exception):
                raise self.narrator
            return SimpleNamespace(text=json.dumps({'narrator': self.narrator}))
        return SimpleNamespace(text=json.dumps(self.entry, ensure_ascii=False))


def request_ids(gateway):
    return [kwargs['request_id'] for _, kwargs in gateway.calls]


def test_the_writer_reads_each_side_under_a_name_that_says_whose_words_they_are():
    packet = {'date': '2026-10-07', 'messages': [{'time': '12:00', 'channel': 'letter', 'user': '他的', 'linli': '她的'}]}
    seen = for_writer(packet)
    assert seen['messages'] == [{'time': '12:00', 'channel': 'letter', '他写给你的': '他的', '你写给他的': '她的'}]
    assert packet['messages'][0]['user'] == '他的' and seen['date'] == '2026-10-07'  # the stored packet keeps its keys
    assert for_writer({'date': 'x'}) == {'date': 'x'}


def test_the_draft_prompt_carries_the_named_sides_and_the_rule_for_who_is_who(tmp_path):
    gateway = Gateway(ENTRY)
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    assert asyncio.run(write_day(store, gateway, '2026-10-07', ROWS, persona='p', now=NOW)) == 'written'
    system, content = gateway.calls[0][0][0]['content'], json.loads(gateway.calls[0][0][1]['content'])
    assert '“我”永远是你自己（林离）' in system and '「他写给你的」' in system
    assert [m['他写给你的'][:4] for m in content['messages']] == ['今天练琴', '我喜欢吃']
    assert all('user' not in m and 'linli' not in m for m in content['messages'])


def test_a_name_she_gave_him_is_never_stored_as_his_name_for_her():
    his = '今天练琴练到忘了吃饭，你别笑我'
    mine = '小熊，那你下次定个闹钟'
    assert directed('name', '他叫我小熊', '小熊', his, mine) == '我叫他小熊'
    assert directed('name', 'He calls me Bear', '小熊', his, mine) == 'I call him Bear'
    assert directed('name', '我叫他小林', '你别笑我', his, mine) == '他叫我小林'
    assert directed('name', '我叫他小熊', '小熊', his, mine) == '我叫他小熊'  # already right
    # his events and likes must be in his own words
    assert directed('user_event', '他家的猫睡了一下午', '定个闹钟', his, mine) is None
    assert directed('preference', '他喜欢吃辣', '你别笑我', his, mine) == '他喜欢吃辣'


def test_validate_turns_round_or_drops_facts_quoted_from_the_wrong_side():
    entry = {**ENTRY, 'facts': [
        {'kind': 'name', 'text': '他叫我小熊', 'date': None, 'quote': '小熊'},
        {'kind': 'user_event', 'text': '他家的猫睡了一下午', 'date': None, 'quote': '林小颖今天又睡了一下午'},
        {'kind': 'preference', 'text': '他喜欢吃辣', 'date': None, 'quote': '我喜欢吃辣'}]}
    facts = validate(entry, day_messages(ROWS, '2026-10-07'))['facts']
    assert [(f['kind'], f['text']) for f in facts] == [('name', '我叫他小熊'), ('preference', '他喜欢吃辣')]


@pytest.mark.real_narrator_check
def test_a_draft_told_from_his_side_is_not_kept(tmp_path):
    gateway = Gateway(ENTRY, narrator='him')
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    with pytest.raises(ValueError, match='DIARY_TOLD_FROM_HIS_SIDE'):
        asyncio.run(write_day(store, gateway, '2026-10-07', ROWS, persona='p', now=NOW))
    assert store.entry('2026-10-07') is None
    assert request_ids(gateway) == ['diary:2026-10-07', 'diary-check:2026-10-07']
    check = gateway.calls[1]
    assert [kwargs['model'] for _, kwargs in gateway.calls] == ['m', 'qwen3.7-flash']
    packet = json.loads(check[0][1]['content'])
    assert packet['diary'] == ENTRY['body'] and '他写给你的' in packet['messages'][0]


@pytest.mark.real_narrator_check
def test_her_entry_is_checked_after_the_draft_and_after_the_edit(tmp_path):
    gateway = Gateway(ENTRY)
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    assert asyncio.run(write_day(store, gateway, '2026-10-07', ROWS, persona='p', now=NOW)) == 'written'
    assert request_ids(gateway) == ['diary:2026-10-07', 'diary-check:2026-10-07',
                                    'diary-revise:2026-10-07', 'diary-check:2026-10-07']


@pytest.mark.real_narrator_check
def test_an_edit_told_from_his_side_falls_back_to_the_draft(tmp_path):
    class Turning(Gateway):
        async def complete_structured_scoped(self, messages, **kwargs):
            if kwargs['request_id'].startswith('diary-revise'):
                self.entry = {**ENTRY, 'body': BODY.replace('他', '她').replace('我', '他').replace('她', '我')}
            if '-check:' in kwargs['request_id']:
                self.narrator = 'him' if len([c for c in self.calls if '-check:' in c[1]['request_id']]) else 'linli'
            return await super().complete_structured_scoped(messages, **kwargs)

    gateway = Turning(ENTRY)
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    assert asyncio.run(write_day(store, gateway, '2026-10-07', ROWS, persona='p', now=NOW)) == 'written'
    assert store.entry('2026-10-07')['body'] == BODY
    assert request_ids(gateway)[-2:] == ['diary-revise:2026-10-07', 'diary-check:2026-10-07']  # the edit was read


@pytest.mark.real_narrator_check
@pytest.mark.parametrize('reading', ['unclear', 'linli', RuntimeError('relay down'), 'nonsense'])
def test_a_failed_or_unsure_check_never_costs_the_entry(reading):
    gateway = Gateway(ENTRY, narrator=reading)
    assert asyncio.run(told_by_her(gateway, 'diary-check:2026-10-07', ENTRY['body'],
                                   day_messages(ROWS, '2026-10-07'), 30)) is True


@pytest.mark.real_narrator_check
def test_another_endpoint_is_not_asked_for_the_relay_reader():
    gateway = Gateway(ENTRY, narrator='him')
    gateway.config.base_url = 'https://example.invalid/v1'
    assert asyncio.run(told_by_her(gateway, 'diary-check:2026-10-07', ENTRY['body'], [], 30)) is True
    assert gateway.calls == []


def saved(store, day, facts=()):
    assert store.save(day, {**ENTRY, 'facts': list(facts)}, now=NOW, model='m', short=False)


def stored_facts(store, day):
    with sqlite3.connect(store.path) as db:
        return json.loads(db.execute('SELECT facts FROM diary_entries WHERE day=?', (day,)).fetchone()[0])


def test_a_rewrite_keeps_his_comments_and_the_old_entry_and_comes_first(tmp_path):
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    saved(store, '2026-09-01')
    store.add_comment('2026-09-01', '写反了', NOW)
    store.request_rewrite('2026-09-01')
    assert store.entry('2026-09-01') is None
    assert due_days(store, NOW)[0] == '2026-09-01'  # older than the caught-up days, still written again
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT text FROM diary_comments WHERE day=?', ('2026-09-01',)).fetchall() == [('写反了',)]
        old = json.loads(db.execute('SELECT entry FROM diary_replaced WHERE day=?', ('2026-09-01',)).fetchone()[0])
    assert old['body'] == ENTRY['body']
    saved(store, '2026-09-01')
    assert store.entry('2026-09-01')['comments'][0]['text'] == '写反了'
    assert '2026-09-01' not in due_days(store, NOW)


def test_only_a_written_day_can_be_written_again(tmp_path):
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    with pytest.raises(KeyError):
        store.request_rewrite('2026-10-07')
    store.save('2026-10', {**ENTRY, 'facts': []}, now=NOW, model='m', short=False)
    with pytest.raises(KeyError):
        store.request_rewrite('2026-10')  # a month's memoir is not a day's entry


def test_stored_facts_are_turned_round_once(tmp_path):
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    saved(store, '2026-10-07', [{'kind': 'name', 'text': '他叫我小熊', 'date': None, 'quote': '小熊'},
                                {'kind': 'user_event', 'text': '他家的猫睡了一下午', 'date': None,
                                 'quote': '林小颖今天又睡了一下午'},
                                {'kind': 'preference', 'text': '他喜欢吃辣', 'date': None, 'quote': '我喜欢吃辣'}])
    saved(store, '2026-10-06', [{'kind': 'name', 'text': '他叫我小熊', 'date': None, 'quote': '小熊'}])  # no messages
    assert store.fix_fact_directions(ROWS) == 1
    facts = stored_facts(store, '2026-10-07')
    assert [(f['kind'], f['text']) for f in facts] == [('name', '我叫他小熊'), ('preference', '他喜欢吃辣')]
    assert stored_facts(store, '2026-10-06')[0]['text'] == '他叫我小熊'
    saved(store, '2026-10-05', [{'kind': 'name', 'text': '他叫我小熊', 'date': None, 'quote': '小熊'}])
    assert store.fix_fact_directions(ROWS + [letter('x', '小熊', -46)]) == 0  # only once
