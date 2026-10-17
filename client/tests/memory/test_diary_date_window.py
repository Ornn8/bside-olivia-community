"""Synthetic source boundaries, independent of when the diary job runs."""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from runtime.diary.diary import (
    DiaryStore, SHANGHAI, day_life, day_messages, due_days, sent_material, write_day,
)


def at(day, clock):
    return datetime.fromisoformat(f'2026-10-{day}T{clock}:00').replace(tzinfo=SHANGHAI)


def letter(day, clock, text):
    return dict(letter_id=text, content=text, reply_text='', letter_status='COMPLETED',
                life_received_at=at(day, clock).isoformat())


class Writer:
    config = SimpleNamespace(model='synthetic')

    def __init__(self):
        self.packets = []

    async def complete_structured_scoped(self, messages, **kwargs):
        self.packets.append(json.loads(messages[1]['content']))
        entry = dict(title='Test', mood='calm', body='Synthetic diary. ' * 6)
        if 'draft' not in self.packets[-1]:
            entry['facts'] = []
        return SimpleNamespace(text=json.dumps(entry))


def test_late_generation_excludes_next_morning_in_both_passes(tmp_path):
    rows = [letter(16, '23:50', 'late yesterday'), letter(17, '07:50', 'morning noodles'),
            letter(16, '00:00', 'start'), letter(17, '00:00', 'end')]
    writer = Writer()
    asyncio.run(write_day(DiaryStore(tmp_path / 'diary.db'), writer, '2026-10-16', rows,
                         persona='static', now=at(17, '09:00')))
    assert len(writer.packets) == 2
    for packet in writer.packets:
        assert [m['user'] for m in packet['messages']] == ['start', 'late yesterday']
        assert 'morning noodles' not in json.dumps(packet)


def test_reply_uses_its_own_timestamp():
    row = dict(letter(16, '23:50', 'yesterday request'), reply_text='morning reply',
               private_world_occurred_at=at(17, '07:50').isoformat())
    assert [(m['user'], m['linli']) for m in day_messages([row], '2026-10-16')] == [('yesterday request', '')]
    assert [(m['user'], m['linli']) for m in day_messages([row], '2026-10-17')] == [('', 'morning reply')]


def test_merged_qq_uses_original_receipt_dates():
    first = dict(letter(16, '23:50', 'first'), channel='qq', binding_id='test',
                 delivery_status='DELIVERED', reply_revision=1, reply_text='morning reply',
                 source_messages={'one': 'first', 'two': 'morning noodles'},
                 content='first\nmorning noodles', private_world_occurred_at=at(17, '07:50').isoformat())
    second = dict(letter(17, '07:50', 'second'), channel='qq', binding_id='test',
                  delivery_status='SKIPPED', source_messages={'two': 'morning noodles'},
                  superseded_by=first['letter_id'])
    yesterday = day_messages([first, second], '2026-10-16')
    today = day_messages([first, second], '2026-10-17')
    assert [m['user'] for m in yesterday] == ['first']
    assert 'morning' not in json.dumps(yesterday)
    assert '\n'.join(m['user'] for m in today).strip() == 'morning noodles'
    assert sum(m['linli'] == 'morning reply' for m in today) == 1


def test_catch_up_two_days_has_no_other_day_memories(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    old = dict(title='old', mood='calm', body='historical material', facts=[
        dict(kind='plan', text='old plan', date='2026-10-16', quote='old plan')])
    store.save('2026-10-14', old, now=at(17, '09:00'), model='test', short=True)
    for day in range(10, 14):
        store.skip(f'2026-10-{day}', 'EMPTY_DAY')
    rows = [letter(15, '23:50', 'day fifteen'), letter(16, '23:50', 'day sixteen'),
            letter(17, '07:50', 'morning noodles')]
    days = due_days(store, at(17, '09:00'))
    assert days == ['2026-10-15', '2026-10-16']
    for day, text in zip(days, ['day fifteen', 'day sixteen']):
        writer = Writer()
        asyncio.run(write_day(store, writer, day, rows, persona='static', now=at(17, '09:00')))
        for packet in writer.packets:
            assert [m['user'] for m in packet['messages']] == [text]
            assert packet['recent_entries'] == packet['due_facts'] == []


def test_world_events_use_actual_instant_not_iso_string_order(tmp_path):
    store = DiaryStore(tmp_path / 'life.db')
    with store._db() as db:
        db.execute('CREATE TABLE life_moments (occurred_at TEXT, kind TEXT, payload TEXT)')
        for stamp, text in [('2026-10-16T23:50:00+08:00', 'late'),
                            ('2026-10-17T07:50:00+08:00', 'next'),
                            ('2026-10-15T16:00:00Z', 'start'),
                            ('2026-10-16T23:59:59.999999+08:00', 'last instant'),
                            ('2026-10-16T16:00:00Z', 'end')]:
            db.execute('INSERT INTO life_moments VALUES (?, ?, ?)',
                       (stamp, 'daily', json.dumps(dict(activity=text))))
    assert [m['activity'] for m in day_life(store, '2026-10-16')] == ['start', 'late', 'last instant']


def test_photos_and_videos_follow_delivery_day(tmp_path):
    photo = tmp_path / 'synthetic.png'
    photo.write_bytes(b'synthetic')
    row = dict(letter(16, '23:50', 'request'), image_delivery_status='DELIVERED',
               image_delivered_at=at(17, '07:50').isoformat(), prepared_image=str(photo),
               image_description={'summary': 'next day photo'})
    videos = [dict(delivery_status='DELIVERED', delivered_at=at(17, '07:50').isoformat(),
                   input={'spoken_text': 'next day video'})]
    assert sent_material([row], '2026-10-16', videos) == ([], [])
    sent, photos = sent_material([row], '2026-10-17', videos)
    assert [item['kind'] for item in sent] == ['photo', 'daily_video']
    assert len(photos) == 1


@pytest.mark.parametrize('stage', ['draft', 'revising', 'final'])
def test_restart_discards_old_unbounded_checkpoint(tmp_path, stage):
    store = DiaryStore(tmp_path / 'diary.db')
    store.checkpoint('2026-10-16', stage, {'packet': {'messages': ['next day contamination']}})
    writer = Writer()
    restarted = DiaryStore(store.path)
    asyncio.run(write_day(restarted, writer, '2026-10-16', [letter(16, '23:50', 'own day')],
                         persona='static', now=at(17, '09:00')))
    assert len(writer.packets) == 2
    assert all('next day contamination' not in json.dumps(p) for p in writer.packets)
    assert restarted.entry('2026-10-16') is not None


def test_real_tick_catches_up_two_days_separately(tmp_path, monkeypatch):
    import local_server as server
    from runtime.call import store as calls
    from runtime.private_world import daily_life_runtime

    store = DiaryStore(tmp_path / 'diary.db')
    for day in range(10, 15):
        store.skip(f'2026-10-{day}', 'EMPTY_DAY')
    writer = Writer()
    monkeypatch.setattr(server, 'diary_store', store)
    monkeypatch.setattr(server, 'store', SimpleNamespace(letters=[
        letter(15, '23:50', 'fifteen'), letter(16, '23:50', 'sixteen'),
        letter(17, '07:50', 'next morning')], personal_chats=[]))
    monkeypatch.setattr(server, 'letters_adapter', SimpleNamespace(gateway=writer, persona_v2_path=None))
    monkeypatch.setattr(server, 'daily_life_runtime', None)
    monkeypatch.setattr(server, '_diary_extras', lambda day: ('unknown', [], None))
    monkeypatch.setattr(server, '_diary_visits', lambda day: [])
    monkeypatch.setattr(calls, 'diary_rows', lambda server: [])
    monkeypatch.setattr(daily_life_runtime, 'life_persona', lambda path: 'static')
    asyncio.run(server._diary_tick(at(17, '09:00')))
    assert [p['date'] for p in writer.packets] == ['2026-10-15'] * 2 + ['2026-10-16'] * 2
    assert [[m['user'] for m in p['messages']] for p in writer.packets] == [['fifteen']] * 2 + [['sixteen']] * 2


@pytest.mark.parametrize('value', ['invalid', '2026-10-17T07:50:00', float('inf')])
def test_invalid_reply_time_does_not_backdate_it(value):
    row = dict(letter(16, '23:50', 'own day'), reply_text='unattributed reply', replied_at=value)
    assert all(not item['linli'] for item in day_messages([row], '2026-10-16'))


def test_cross_midnight_call_without_utterance_times_is_not_backdated():
    from runtime.call.store import diary_rows
    server = SimpleNamespace(store=SimpleNamespace(calls=[dict(
        started_at=at(16, '23:50').isoformat(), ended_at=at(17, '00:10').isoformat(),
        transcript=[{'role': 'user', 'text': 'unknown side of midnight'}])]))
    assert diary_rows(server) == []


def test_visits_follow_shanghai_day(tmp_path):
    from runtime.cloud_events import record, visits
    record(tmp_path, [{'id': str(i), 'at': int(stamp.timestamp())} for i, stamp in enumerate([
        at(16, '23:50'), at(17, '00:00'), at(17, '07:50')])], int(at(17, '09:00').timestamp()))
    assert visits(tmp_path, '2026-10-16') == ['23:50']
    assert visits(tmp_path, '2026-10-17') == ['00:00', '07:50']
