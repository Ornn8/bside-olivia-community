import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from runtime.diary.diary import (DiaryStore, MAX_ATTEMPTS, SHANGHAI, day_messages, due_days, validate, write_day)

NOW = datetime(2026, 10, 8, 4, 0, tzinfo=SHANGHAI)


def chat(content, reply, at, **extra):
    return {'letter_id': at.isoformat(), 'channel': 'qq', 'content': content, 'reply_text': reply,
            'delivery_status': 'DELIVERED', 'life_received_at': at.isoformat(), **extra}


def letter(content, reply, at, status='COMPLETED'):
    return {'letter_id': 'l' + at.isoformat(), 'content': content, 'reply_text': reply,
            'letter_status': status, 'life_received_at': at.isoformat()}


DAY = datetime(2026, 10, 7, tzinfo=SHANGHAI)
ROWS = [
    chat('小猫咪，下周三我要考乐理，好紧张，今天复习了一下午还是记不住转调', '我陪你复习呀', DAY + timedelta(hours=20)),
    letter('还记得8月26号吗？那天我们说好把它当纪念日', '当然记得', DAY + timedelta(hours=9)),
    letter('这封失败了不该出现', '', DAY + timedelta(hours=10), status='FAILED'),
    chat('昨天的消息', '嗯', DAY - timedelta(hours=1)),
    chat('明天的消息', '嗯', DAY + timedelta(days=1, hours=1)),
]


class Gateway:
    def __init__(self, value):
        self.value, self.calls = value, []
        self.config = SimpleNamespace(model='claude-sonnet-5-5')

    async def complete_structured_scoped(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return SimpleNamespace(text=json.dumps(self.value, ensure_ascii=False))


ENTRY = {'title': '下周三的乐理', 'mood': '心疼', 'body': '你说下周三要考乐理，我听出你有点紧张……',
         'facts': [{'kind': 'user_event', 'text': '对方下周三考乐理', 'date': '2026-10-14', 'quote': '下周三我要考乐理'},
                   {'kind': 'anniversary', 'text': '8月26日是我们的纪念日', 'date': '2026-08-26', 'quote': '把它当纪念日'},
                   {'kind': 'promise', 'text': '我答应周末陪对方去看海', 'date': None, 'quote': '周末一起去看海'}]}


def test_day_messages_take_only_that_days_delivered_exchanges_in_order():
    items = day_messages(ROWS, '2026-10-07')
    assert [m['user'][:4] for m in items] == ['还记得8', '小猫咪，']
    assert items[0]['channel'] == 'letter' and items[0]['time'] == '09:00'


def test_facts_without_a_verbatim_quote_from_that_day_are_dropped():
    entry = validate(ENTRY, day_messages(ROWS, '2026-10-07'))
    assert [f['kind'] for f in entry['facts']] == ['user_event', 'anniversary']  # the invented promise is gone


def test_write_day_saves_once_and_feeds_reply_context(tmp_path):
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    gateway = Gateway(ENTRY)
    assert asyncio.run(write_day(store, gateway, '2026-10-07', ROWS, persona='persona', now=NOW)) == 'written'
    prompt = json.loads(gateway.calls[0][0][1]['content'])
    assert prompt['short_day'] is False and len(prompt['messages']) == 2
    assert gateway.calls[0][1]['request_id'] == 'diary:2026-10-07'
    assert not store.may_attempt('2026-10-07')
    context = json.loads(store.context(NOW))
    assert context['recent_entries'][0]['title'] == '下周三的乐理'
    assert {f['text'] for f in context['facts']} == {'对方下周三考乐理', '8月26日是我们的纪念日'}
    assert asyncio.run(write_day(store, gateway, '2026-10-06', [], persona='p', now=NOW)) == 'empty'


def test_short_day_is_marked_short():
    store_rows = [chat('早', '早呀', DAY + timedelta(hours=8))]
    gateway = Gateway({**ENTRY, 'facts': []})
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as folder:
        store = DiaryStore(pathlib.Path(folder) / 'diary.sqlite3')
        asyncio.run(write_day(store, gateway, '2026-10-07', store_rows, persona='p', now=NOW))
        assert json.loads(gateway.calls[0][0][1]['content'])['short_day'] is True
        assert store.page()['entries'][0]['short'] is True


def test_comments_seen_and_delete_removes_the_days_facts(tmp_path):
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    asyncio.run(write_day(store, Gateway(ENTRY), '2026-10-07', ROWS, persona='p', now=NOW))
    page = store.page()
    assert page['unseen'] == 1 and page['entries'][0]['commented'] is False
    store.add_comment('2026-10-07', '看到啦，谢谢你', NOW)
    store.mark_seen('2026-10-07')
    assert store.page()['unseen'] == 0 and store.page()['entries'][0]['commented'] is True
    assert json.loads(store.context(NOW))['recent_entries'][0]['user_comment'] == '看到啦，谢谢你'
    with pytest.raises(ValueError):
        store.add_comment('2026-10-07', ' ', NOW)
    with pytest.raises(KeyError):
        store.add_comment('2026-10-01', '没有这篇', NOW)
    store.delete('2026-10-07')
    assert store.context(NOW) is None and store.entry('2026-10-07') is None
    assert not store.may_attempt('2026-10-07')  # a deleted day is never rewritten
    assert not store.save('2026-10-07', validate(ENTRY, day_messages(ROWS, '2026-10-07')), now=NOW, model='m', short=False)


def test_due_days_wait_for_one_am_and_respect_attempts(tmp_path):
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    before = datetime(2026, 10, 8, 0, 30, tzinfo=SHANGHAI)
    assert due_days(store, before)[-1] == '2026-10-06'
    assert due_days(store, NOW)[-1] == '2026-10-07' and len(due_days(store, NOW)) == 7
    for _ in range(MAX_ATTEMPTS):
        store.failed('2026-10-07', 'GatewayError')
    assert '2026-10-07' not in due_days(store, NOW)
    store.skip('2026-10-06', 'EMPTY_DAY')
    assert '2026-10-06' not in due_days(store, NOW)


def test_disabled_setting_and_invalid_values(tmp_path):
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    assert store.enabled()
    store.set_enabled(False)
    assert not store.enabled()
    with pytest.raises(ValueError):
        store.set_enabled('no')
    with pytest.raises(ValueError):
        store.page(page=0)


def test_memoir_months_write_once_and_keep_facts_out_of_recent_entries(tmp_path):
    from runtime.diary.diary import memoir_months, month_messages, write_month
    aug = datetime(2026, 8, 26, 21, tzinfo=SHANGHAI)
    rows = [chat('今天是我们一起过的生日，小猫咪唱了两首生日歌，说好8月26号就是纪念日', '嗯，我记住了', aug),
            letter('导入的旧信没有日期', '回信', datetime(1970, 1, 1, tzinfo=timezone.utc)),
            chat('这个月的消息', '嗯', NOW)]
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    assert [m['month'] for m in memoir_months(store, rows, NOW)] == ['2026-08']
    assert [m['time'] for m in month_messages(rows, '2026-08')] == ['08-26 21:00']
    entry = {'title': '那年的生日', 'mood': '甜', 'body': '那天你说……',
             'facts': [{'kind': 'anniversary', 'text': '8月26日是纪念日', 'date': '2026-08-26', 'quote': '说好8月26号就是纪念日'}]}
    gateway = Gateway(entry)
    assert asyncio.run(write_month(store, gateway, '2026-08', rows, persona='p', now=NOW)) == 'written'
    assert gateway.calls[0][1]['request_id'] == 'diary-memoir:2026-08'
    assert memoir_months(store, rows, NOW) == []
    store.save('2026-10-07', validate(ENTRY, day_messages(ROWS, '2026-10-07')), now=NOW, model='m', short=False)
    context = json.loads(store.context(NOW))
    assert [e['day'] for e in context['recent_entries']] == ['2026-10-07']
    assert '8月26日是纪念日' in {f['text'] for f in context['facts']}
    assert store.page()['entries'][1]['day'] == '2026-08'


def test_due_facts_repeat_anniversaries_yearly_and_dated_facts_on_their_day(tmp_path):
    from runtime.diary.diary import due_facts
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    facts = [{'kind': 'anniversary', 'text': '纪念日', 'date': '2025-10-08', 'quote': 'q'},
             {'kind': 'user_event', 'text': '今天考乐理', 'date': '2026-10-08', 'quote': 'q'},
             {'kind': 'user_event', 'text': '明天体检', 'date': '2026-10-09', 'quote': 'q'},
             {'kind': 'name', 'text': '叫她小猫咪', 'date': None, 'quote': 'q'}]
    store.save('2026-10-01', {'title': 't', 'mood': 'm', 'body': 'b', 'facts': facts}, now=NOW, model='m', short=False)
    assert {f['text'] for f in due_facts(store, NOW)} == {'纪念日', '今天考乐理'}
