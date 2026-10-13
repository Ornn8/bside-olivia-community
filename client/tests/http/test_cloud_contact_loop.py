"""The client's own loop that carries out the cloud's contacts: wiring, channels and lifecycle."""
import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from runtime import cloud_contact, proactive_config
from runtime.cloud_contact import Outcome
from tests.http.test_expression_context_routes import run_isolated

BEIJING = timezone(timedelta(hours=8))
NOW = int(datetime(2026, 10, 13, 20, tzinfo=BEIJING).timestamp())
ID = 'contact:00000000-0000-4000-8000-000000000001'


def contact(contact_id=ID, **fields):
    return cloud_contact.parse_event({'id': contact_id, 'kind': 'contact', 'at': NOW - 600, 'deliver_after': NOW - 60,
                                      'expires': NOW + 3600, 'purpose': 'recall', 'channel': 'preferred',
                                      'brief': '说两句', 'qq_format': 'text', **fields})


@pytest.fixture
def wired(monkeypatch, tmp_path):
    import local_server
    proactive_config.configure(None)
    sent = []
    async def qq(event):
        sent.append(('qq', event['id']))
        return Outcome('delivered')
    async def letter(event):
        sent.append(('letter', event['id']))
        return Outcome('delivered')
    monkeypatch.setattr(local_server, '_state_root', lambda: tmp_path)
    monkeypatch.setattr(local_server, '_deliver_contact_letter', letter)
    monkeypatch.setattr(local_server, '_qq_contact_reachable', lambda: True)
    monkeypatch.setattr(local_server, '_qq_contact_deliver', qq)
    monkeypatch.setattr(local_server, '_proactive_settings', lambda: {'enabled': True})
    monkeypatch.setattr(local_server, '_jev_contact_channel', None, raising=False)
    monkeypatch.setattr(local_server.time, 'time', lambda: NOW)
    async def no_refresh(now=None):
        return False
    monkeypatch.setattr(proactive_config, 'refresh_if_due', no_refresh)
    async def post(payload):
        sent.append(('ack', payload['status']))
    monkeypatch.setattr(cloud_contact, '_send_ack', post)
    yield SimpleNamespace(server=local_server, sent=sent, root=tmp_path)
    proactive_config.configure(None)


def owner_rows(server, monkeypatch, qq, letters):
    def make(kind, number):
        base = {'letter_id': f'{kind}{number}', 'content': '在吗', 'created_at': NOW - 3600 - number, 'letter_status': 'COMPLETED',
                'reply_text': '在'}
        return {**base, 'channel': 'qq', 'binding_id': 'b', 'source_messages': {f'{kind}{number}': 'x'},
                'delivery_status': 'DELIVERED'} if kind == 'qq' else base
    monkeypatch.setattr(server.store, 'personal_chats', [make('qq', i) for i in range(qq)])
    monkeypatch.setattr(server.store, 'letters', [make('letter', i) for i in range(letters)])


def run_tick(env, *events):
    cloud_contact.record(env.root, list(events), NOW)
    asyncio.run(env.server._contact_tick())
    asyncio.run(env.server._contact_tick())  # The second pass reports what the first one did.


def test_a_contact_on_qq_goes_to_the_chat_backend(wired):
    run_tick(wired, contact(channel='qq'))
    assert wired.sent == [('qq', ID), ('ack', 'delivered')]


def test_a_letter_contact_goes_to_the_letter_pipeline_even_when_qq_is_there(wired):
    run_tick(wired, contact(channel='letter'))
    assert wired.sent == [('letter', ID), ('ack', 'delivered')]


def test_qq_that_cannot_be_reached_never_receives_a_qq_contact(wired, monkeypatch):
    monkeypatch.setattr(wired.server, '_qq_contact_reachable', None)
    run_tick(wired, contact(channel='qq'))
    assert wired.sent == [] and [e['id'] for e in cloud_contact.pending(wired.root)] == [ID]
    run_tick(wired, contact(ID.replace('1', '2'), channel='qq_then_letter', qq_wait_seconds=0))
    assert wired.sent[0] == ('letter', ID.replace('1', '2'))


def test_preferred_follows_the_channel_the_owner_writes_on_most(wired, monkeypatch):
    owner_rows(wired.server, monkeypatch, qq=6, letters=1)
    run_tick(wired, contact(channel='preferred'))
    assert wired.sent[0] == ('qq', ID)
    wired.sent.clear()
    owner_rows(wired.server, monkeypatch, qq=1, letters=6)
    other = ID.replace('1', '2')
    run_tick(wired, contact(other, channel='preferred'))
    assert wired.sent[0] == ('letter', other)


def test_preferred_follows_the_clouds_channel_policy_and_falls_back_to_a_letter(wired, monkeypatch):
    owner_rows(wired.server, monkeypatch, qq=6, letters=1)
    proactive_config.adopt({'version': 'v', 'rules': {'channel_policy': 'letter_first'}})
    run_tick(wired, contact(channel='preferred'))
    assert wired.sent[0] == ('letter', ID)
    wired.sent.clear()
    monkeypatch.setattr(wired.server, '_qq_contact_reachable', None)
    proactive_config.adopt({'version': 'v', 'rules': {'channel_policy': 'qq_first'}})
    monkeypatch.setattr(wired.server, '_proactive_settings', lambda: {'enabled': False})
    other = ID.replace('1', '2')
    run_tick(wired, contact(other, channel='preferred'))
    assert wired.sent[0] == ('letter', other)  # Nothing else can reach her, and a contact still does.


def test_a_contact_ignores_the_owners_own_proactive_letter_switch(wired, monkeypatch):
    monkeypatch.setattr(wired.server, '_proactive_settings', lambda: {'enabled': False})
    run_tick(wired, contact(channel='letter'))
    assert wired.sent[0] == ('letter', ID)


def test_her_own_initiative_in_progress_holds_the_contact_for_the_next_pass(wired):
    wired.server._jev_contact_channel = 'letter'
    cloud_contact.record(wired.root, [contact(channel='letter')], NOW)
    asyncio.run(wired.server._contact_tick())
    assert wired.sent == []
    wired.server._jev_contact_channel = None
    asyncio.run(wired.server._contact_tick())
    assert wired.sent == [('letter', ID)] and wired.server._jev_contact_channel is None


def test_the_config_is_looked_at_on_every_pass(wired, monkeypatch):
    seen = []
    async def refresh(now=None):
        seen.append(1)
        return False
    monkeypatch.setattr(proactive_config, 'refresh_if_due', refresh)
    asyncio.run(wired.server._contact_tick())
    assert seen == [1]


def test_the_loop_waits_a_while_then_checks_every_half_minute_and_survives_an_error(wired, monkeypatch):
    sleeps, ticks = [], []
    async def tick():
        ticks.append(1)
        if len(ticks) == 1:
            raise OSError('synthetic')
    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 3:
            raise asyncio.CancelledError()
    monkeypatch.setattr(wired.server, '_contact_tick', tick)
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(wired.server._contact_loop())
    assert sleeps[:3] == [45, 30, 30] and len(ticks) == 3


def test_the_loop_starts_with_the_reply_tasks_and_stops_with_them(tmp_path):
    (tmp_path / 'proactive').mkdir()
    (tmp_path / 'proactive' / 'config.json').write_text(json.dumps(
        {'version': 'kept-1', 'rules': {'max_per_day': 2}}), encoding='utf-8')
    run_isolated(tmp_path, r'''
import asyncio
import local_server as server
from runtime import proactive_config
server._refresh_proactive_context = lambda *a, **k: None
server._proactive_settings = lambda: {'enabled': False, 'login_check_enabled': False}
server._schedule_pending_reply_jobs = lambda: None
server._schedule_pending_media_jobs = lambda: None
server.daily_life_runtime = None
async def idle():
    await asyncio.sleep(60)
server._proactive_loop = idle
async def check():
    await server._start_reply_tasks(None)
    await asyncio.sleep(0)
    assert server._contact_task is not None and not server._contact_task.done()
    assert proactive_config.status() == {'version': 'kept-1', 'source': 'cache'}   # The kept answer is in use from the start.
    assert proactive_config.rule('max_per_day') == 2
    task = server._contact_task
    await server._stop_reply_tasks(None)
    assert server._contact_task is None and task.cancelled()
asyncio.run(check())
''')
