"""A top-up is no longer answered at once: only its local time is kept, for the diary."""
import asyncio
import json
import re
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from runtime import cloud_events
from runtime.diary.diary import DiaryStore, SHANGHAI, write_day, write_month
from tests.memory.test_diary import ENTRY, NOW, ROWS

ID = 'gift:00000000-0000-4000-8000-000000000001'
OTHER = 'gift:00000000-0000-4000-8000-000000000002'
BRIEF = 'SYNTHETIC_BRIEF_TEXT'
EXTRAS = dict(amount_cents=731900, currency='XXX', order='o-731900', note='SYNTHETIC_NOTE')
WORDS = re.compile(r'充值|余额|支付|金额|付款|top-?up|recharge|balance|payment|amount|currency', re.I)


def moment(day, hour, minute):
    """An epoch second for a Shanghai wall-clock time, so the assertions can name the local time."""
    year, month, date = map(int, day.split('-'))
    return int(datetime(year, month, date, hour, minute, tzinfo=SHANGHAI).timestamp())


def cloud_event(event_id, at, **extra):
    return {'id': event_id, 'kind': 'gift', 'at': at, 'brief': BRIEF, 'photo': True, **extra}


def test_only_the_id_and_the_moment_are_read_from_an_event():
    at = moment('2026-10-07', 21, 40)
    events = [cloud_event(ID, at, **EXTRAS),
              {**cloud_event(OTHER, at), 'kind': 'newer'},
              cloud_event('../x', at), cloud_event(OTHER, True), cloud_event(OTHER, -5)]
    assert cloud_events.validate({'events': events}) == [{'id': ID, 'at': at}]
    assert cloud_events.validate({'events': [{'id': ID, 'kind': 'gift', 'at': at}]}) == [{'id': ID, 'at': at}]
    with pytest.raises(ValueError, match='CLOUD_EVENTS_INVALID'):
        cloud_events.validate({'events': 'x'})


def test_the_file_keeps_ids_and_moments_and_nothing_else(tmp_path):
    at = moment('2026-10-07', 21, 40)
    cloud_events.record(tmp_path, cloud_events.validate({'events': [cloud_event(ID, at, **EXTRAS)]}), at + 60)
    cloud_events.record(tmp_path, cloud_events.validate({'events': [cloud_event(ID, at)]}), at + 120)  # counted once
    text = (tmp_path / 'proactive' / 'cloud-events.json').read_text(encoding='utf-8')
    assert json.loads(text) == {'visits': {ID: at}}
    assert not WORDS.search(text) and not any(token in text for token in (BRIEF, '731900', 'XXX', 'SYNTHETIC_NOTE'))


def test_what_an_earlier_version_left_is_read_for_its_moments_and_rewritten_without_the_rest(tmp_path):
    at = moment('2026-10-07', 8, 5)
    path = tmp_path / 'proactive' / 'cloud-events.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'events': {ID: {'id': ID, 'kind': 'gift', 'at': at, 'seen_at': at, 'status': 'pending',
                                                'brief': BRIEF, 'photo': True}}}), encoding='utf-8')
    assert cloud_events.visits(tmp_path, '2026-10-07') == ['08:05']
    cloud_events.record(tmp_path, [], at + 60)
    assert json.loads(path.read_text(encoding='utf-8')) == {'visits': {ID: at}}


def test_visits_are_local_times_of_that_day_or_month_oldest_first(tmp_path):
    cloud_events.record(tmp_path, [{'id': OTHER, 'at': moment('2026-10-07', 21, 40)},
                                   {'id': ID, 'at': moment('2026-10-07', 0, 5)},
                                   {'id': 'gift:00000000-0000-4000-8000-000000000003', 'at': moment('2026-10-08', 9, 30)},
                                   {'id': 'gift:00000000-0000-4000-8000-000000000004', 'at': moment('2026-09-30', 23, 59)}],
                        moment('2026-10-09', 12, 0))
    assert cloud_events.visits(tmp_path, '2026-10-07') == ['00:05', '21:40']
    assert cloud_events.visits(tmp_path, '2026-10-08') == ['09:30']
    assert cloud_events.visits(tmp_path, '2026-10-06') == []
    assert cloud_events.visits(tmp_path, '2026-10') == ['10-07 00:05', '10-07 21:40', '10-08 09:30']
    assert cloud_events.visits(tmp_path, '2026-09') == ['09-30 23:59']
    assert cloud_events.visits(None, '2026-10-07') == [] and cloud_events.visits(tmp_path / 'none', '2026-10-07') == []


def test_old_moments_are_forgotten_after_a_long_time(tmp_path):
    at = moment('2026-10-07', 21, 40)
    cloud_events.record(tmp_path, [{'id': ID, 'at': at}], at + 60)
    cloud_events.record(tmp_path, [], at + cloud_events.KEEP_SECONDS + 60)
    assert cloud_events.visits(tmp_path, '2026-10-07') == []


def writer_calls(tmp_path, visits=None, *, month=False):
    from tests.memory.test_diary_quality import Writer
    store = DiaryStore(tmp_path / 'diary.db')
    writer = Writer({'title': '改好了', 'mood': '暖', 'body': '今天的具体画面。' * 35})
    extra = {} if visits is None else {'visits': visits}
    if month:
        asyncio.run(write_month(store, writer, '2026-10', ROWS, persona='p', now=NOW, **extra))
    else:
        asyncio.run(write_day(store, writer, '2026-10-07', ROWS, persona='p', now=NOW, **extra))
    return [json.loads(call[0][1]['content']) for call in writer.calls], writer


def test_the_days_packet_carries_the_visits_as_times_and_the_revision_reuses_it(tmp_path):
    draft, revision = writer_calls(tmp_path, ['08:05', '21:40'])[0]
    assert draft['his_visits'] == [{'time': '08:05'}, {'time': '21:40'}]
    assert revision['his_visits'] == draft['his_visits'] and 'draft' in revision


def test_a_day_without_visits_has_no_such_key(tmp_path):
    for name, visits in (('none', None), ('empty', [])):
        packets, _ = writer_calls(tmp_path / name, visits)
        assert len(packets) == 2 and all('his_visits' not in packet for packet in packets)


def test_the_month_memoir_gets_dated_times(tmp_path):
    packets, _ = writer_calls(tmp_path, ['10-07 21:40'], month=True)
    assert packets[0]['month'] == '2026-10' and packets[0]['his_visits'] == [{'time': '10-07 21:40'}]


def test_nothing_that_reaches_a_model_says_anything_about_a_top_up(tmp_path):
    """From the cloud payload to the writer's request: the amount, the brief and the order never leave the file."""
    at = moment('2026-10-07', 21, 40)
    payload = {'events': [cloud_event(ID, at, **EXTRAS)]}
    cloud_events.record(tmp_path, cloud_events.validate(payload), at + 60)
    visits = cloud_events.visits(tmp_path, '2026-10-07')
    packets, writer = writer_calls(tmp_path / 'diary', visits)
    for packet in packets:
        assert packet['his_visits'] == [{'time': '21:40'}]
        assert set(packet['his_visits'][0]) == {'time'}
    wire = json.dumps([call[0] for call in writer.calls], ensure_ascii=False)
    for leaked in (BRIEF, '731900', 'XXX', 'o-731900', 'SYNTHETIC_NOTE', ID, 'gift:'):
        assert leaked not in wire
    assert not WORDS.search(json.dumps([packet['his_visits'] for packet in packets], ensure_ascii=False))
    # The only place the field exists is its own key, and its values are clock times.
    assert all(re.fullmatch(r'\d\d:\d\d', item['time']) for packet in packets for item in packet['his_visits'])


def test_polling_stores_no_amount_or_wording(tmp_path, monkeypatch):
    import original_client_relay_api as relay
    at = int(time.time()) - 600
    async def fetch(base, key, method, path, payload=None):
        assert (method, path) == ('GET', '/events')
        return {'events': [cloud_event(ID, at, **EXTRAS)]}
    monkeypatch.setattr(relay, 'relay_request', fetch)
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'olivia-synthetic')
    monkeypatch.setenv('OLIVIA_GPU_API_URL', relay.RELAY_BASE.removesuffix('/v1'))
    asyncio.run(cloud_events.poll(tmp_path))
    text = (tmp_path / 'proactive' / 'cloud-events.json').read_text(encoding='utf-8')
    assert json.loads(text) == {'visits': {ID: at}}
    assert not WORDS.search(text) and not any(word in text for word in (BRIEF, '731900', 'XXX', 'brief', 'photo'))


def test_a_top_up_sends_no_message_photo_or_letter_from_the_letter_side(tmp_path, monkeypatch):
    import local_server
    import original_client_relay_api as relay
    at = int(time.time()) - 60
    path = tmp_path / 'proactive' / 'cloud-events.json'
    path.parent.mkdir()
    # An unanswered top-up the previous version would have answered at once with a letter.
    path.write_text(json.dumps({'events': {ID: {'id': ID, 'kind': 'gift', 'at': at, 'seen_at': at, 'status': 'pending',
                                                'brief': BRIEF, 'photo': True}}}), encoding='utf-8')
    async def fetch(base, key, method, path, payload=None):
        return {'events': [cloud_event(OTHER, at + 5)]}
    async def forbidden(*args, **kwargs):
        raise AssertionError('a top-up must not start any contact')
    letters, chats = list(local_server.store.letters), list(local_server.store.personal_chats)
    monkeypatch.setattr(relay, 'relay_request', fetch)
    monkeypatch.setattr(local_server, '_state_root', lambda: tmp_path)
    monkeypatch.setattr(local_server, '_prepare_proactive_turn', forbidden)
    monkeypatch.setattr(local_server, '_publish_proactive', forbidden)
    monkeypatch.setattr(local_server, '_proactive_ready', lambda: True)
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'olivia-synthetic')
    monkeypatch.setenv('OLIVIA_GPU_API_URL', relay.RELAY_BASE.removesuffix('/v1'))
    sleeps = []
    real_sleep = asyncio.sleep
    async def sleep(seconds, *args):
        sleeps.append(seconds)
        if len(sleeps) > 1:
            raise asyncio.CancelledError()
        await real_sleep(0)
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(local_server._visit_loop())
    assert sleeps == [30, cloud_events.POLL_SECONDS]
    assert local_server.store.letters == letters and local_server.store.personal_chats == chats
    assert json.loads(path.read_text(encoding='utf-8')) == {'visits': {ID: at, OTHER: at + 5}}
    assert not hasattr(local_server, '_gift_letter_tick') and not hasattr(local_server, '_gift_loop')


def test_a_top_up_sends_no_message_or_photo_from_the_qq_side(tmp_path, monkeypatch):
    import local_server
    from aiohttp import web
    from runtime.personal_chat import backend, qq
    from runtime.personal_chat.service import PersonalChatService
    assert not hasattr(backend, '_gift_contact')
    monkeypatch.delenv('OLIVIA_PERSONAL_CHAT_CONFIG', raising=False)
    monkeypatch.setenv('OLIVIA_PERSONAL_QQ_TOKEN', 'synthetic-token-123456789')
    folder = tmp_path / 'personal-chat'
    folder.mkdir()
    (folder / 'config.json').write_text(json.dumps({'qq': {'url': 'ws://127.0.0.1:3001', 'account': '100', 'owner': '200'}}),
                                        encoding='utf-8')
    (folder / 'setup-choice.json').write_text(json.dumps({'channels': ['qq']}), encoding='utf-8')
    at = int(time.time()) - 30
    (tmp_path / 'proactive').mkdir()
    (tmp_path / 'proactive' / 'cloud-events.json').write_text(json.dumps({'events': {ID: {
        'id': ID, 'kind': 'gift', 'at': at, 'seen_at': at, 'status': 'pending', 'brief': BRIEF, 'photo': True}}}),
        encoding='utf-8')
    contacts, pictures, rounds = [], [], []
    async def proactive(self, *args, **kwargs):
        contacts.append((args, kwargs))
    monkeypatch.setattr(PersonalChatService, 'proactive', proactive)
    original_round = backend._proactive_contact
    async def counted(*args):
        rounds.append(True)
        return await original_round(*args)
    monkeypatch.setattr(backend, '_proactive_contact', counted)
    sender = SimpleNamespace(is_available=lambda: True, audio=lambda path: None,
                             image=lambda path: pictures.append(path))
    async def listen_qq(url, token, account, owner, handler, stop, **kwargs):
        kwargs.get('state_callback', lambda _state: None)('CONNECTED')
        handler.ready('qq', sender)
        await stop.wait()
    monkeypatch.setattr(qq, 'run_qq', listen_qq)
    server = SimpleNamespace(store=local_server.Store(), _state_root=lambda: tmp_path,
        _require_store_state_available=lambda: None, _persist_store_state=lambda: None,
        _safe_log=lambda *a, **k: None, _atomic_write_store_file=local_server._atomic_write_store_file,
        _voice_reply_configured=lambda _: True)
    real_sleep = asyncio.sleep
    async def sleep(seconds, *args):
        await real_sleep(.01 if seconds == 15 else seconds)  # The initiative loop wakes often.
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    async def scenario():
        app = web.Application()
        backend.install_personal_chat(app, server)
        runner = web.AppRunner(app)
        await runner.setup()
        await real_sleep(.3)
        await runner.cleanup()
    asyncio.run(scenario())
    assert len(rounds) >= 3  # The initiative loop really ran while the unanswered top-up was on file.
    assert contacts == [] and pictures == [] and server.store.personal_chats == []
