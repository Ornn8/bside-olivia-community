"""Contact she starts on her own uses the channel the owner talks on most; replies are untouched."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from runtime.personal_chat import backend
from runtime.personal_chat.engagement import channel_counts, initiating_channel
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.initiative import Initiative
from runtime.personal_chat.initiative_profile import profile_from_rows

BEIJING = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 12, 12, tzinfo=BEIJING).timestamp()
DAY = 86400


def qq_message(name, age_days, sources=1):
    at = NOW - age_days * DAY
    return {'letter_id': name, 'channel': 'qq', 'binding_id': 'b', 'content': '在吗', 'reply_text': '在呢',
            'source_messages': {f'{name}-{i}': '在吗' for i in range(sources)}, 'created_at': at,
            'user_sent_at': None, 'delivery_status': 'DELIVERED', 'letter_status': 'COMPLETED'}


def letter(name, age_days):
    return {'letter_id': name, 'content': '写给你的信', 'created_at': NOW - age_days * DAY,
            'letter_status': 'COMPLETED', 'reply_text': '收到'}


def owner_world(*, qq=(), letters=(), letters_on=True):
    """Chats are stored oldest first; the owner's letters are inserted at the front."""
    return SimpleNamespace(
        store=SimpleNamespace(personal_chats=sorted(qq, key=lambda row: row['created_at']),
                              letters=sorted(letters, key=lambda row: -row['created_at'])),
        _proactive_settings=lambda: {'enabled': letters_on})


def many(kind, count, age=1):
    return [kind(f'{kind.__name__}{i}', age + i * .1) for i in range(count)]


def test_counts_cover_the_last_two_weeks_of_the_owners_own_messages_only():
    world = owner_world(
        qq=[qq_message('a', 1), qq_message('merged', 2, sources=3), qq_message('old', 15),
            {**qq_message('mine', 1), 'origin': 'proactive'}],
        letters=[letter('l1', 3), letter('l2', 13), letter('old', 15), {**letter('hers', 1), 'origin': 'proactive'}])
    assert channel_counts(world, NOW) == {'qq': 4, 'letter': 2}
    # An empty message is not something the owner said.
    world.store.letters.insert(0, {**letter('blank', 0), 'content': ''})
    assert channel_counts(world, NOW)['letter'] == 2


@pytest.mark.parametrize('qq,letters,qq_ready,letters_on,expected', [
    (5, 1, True, True, 'qq'),           # QQ-heavy: QQ starts, letters do not
    (1, 5, True, True, 'letter'),       # letter-heavy: letters start, QQ does not
    (2, 2, True, True, 'qq'),           # tie goes to QQ while it is connected
    (0, 0, True, True, 'qq'),           # nothing written yet
    (2, 2, False, True, 'letter'),      # a tie with QQ not connected goes to letters
    (0, 0, False, True, 'letter'),
    (5, 1, False, True, 'letter'),      # QQ preferred but not connected: letters take over
    (1, 5, True, False, 'qq'),          # letters preferred but switched off: QQ takes over
    (0, 0, False, False, None),         # nobody can start contact
])
def test_the_channel_with_more_of_the_owners_messages_starts_contact(qq, letters, qq_ready, letters_on, expected):
    world = owner_world(qq=many(qq_message, qq), letters=many(letter, letters), letters_on=letters_on)
    assert initiating_channel(world, qq_ready, NOW) == expected


def test_the_choice_follows_the_owner_as_old_messages_age_out():
    world = owner_world(qq=many(qq_message, 6, age=1), letters=many(letter, 3, age=1))
    assert initiating_channel(world, True, NOW) == 'qq'
    # Two weeks on, the chats have left the window and one fresh letter is all that remains.
    later = NOW + 14 * DAY
    world.store.letters.insert(0, {**letter('fresh', 0), 'created_at': later - DAY})
    assert channel_counts(world, later) == {'qq': 0, 'letter': 1}
    assert initiating_channel(world, True, later) == 'letter'


def test_a_replaced_store_is_not_counted_with_the_old_one():
    world = owner_world(qq=many(qq_message, 4))
    assert initiating_channel(world, True, NOW) == 'qq'
    world.store = SimpleNamespace(personal_chats=[], letters=many(letter, 4))
    assert initiating_channel(world, True, NOW) == 'letter'


def initiative_for(world, gate=True, followup=False):
    chats = world.store.personal_chats
    if followup:
        chats.append({**qq_message('promise', .1), 'followup_at': NOW - 60, 'followup_quote': '明早叫我'})
    result = Initiative(chats, clock=lambda: NOW, interval=lambda: 0,
                        profile_provider=lambda: profile_from_rows([]),
                        channel_gate=(lambda: initiating_channel(world, True, NOW) == 'qq') if gate else None)
    result.received(PersonalMessage('qq', 'bot', 'owner', 'prior', '此前的话'), lambda text: None)
    result.due = NOW - 1
    return result


@pytest.mark.parametrize('qq,letters,ready', [(6, 1, True), (1, 6, False), (3, 3, True), (0, 0, True)])
def test_qq_only_starts_contact_when_it_is_the_owners_main_channel(qq, letters, ready):
    world = owner_world(qq=many(qq_message, qq), letters=many(letter, letters))
    initiative = initiative_for(world)
    assert initiative.ready() is ready
    assert initiative._reason() == ('ready' if ready else 'deferred')


def test_without_a_preference_gate_qq_keeps_its_old_behavior():
    world = owner_world(qq=many(qq_message, 1), letters=many(letter, 6))
    assert initiative_for(world, gate=False).ready() is True


def test_a_contact_the_owner_asked_for_keeps_its_channel():
    world = owner_world(qq=many(qq_message, 1), letters=many(letter, 6))
    assert initiative_for(world).ready() is False
    assert initiative_for(owner_world(qq=many(qq_message, 1), letters=many(letter, 6)), followup=True).ready() is True


def contact_world(monkeypatch, tmp_path, world, *, status='CONNECTED'):
    calls = []
    class Service:
        async def proactive(self, event, send, eligible, followup=None, gift=None):
            calls.append(event.exchange_id)
    monkeypatch.setattr(backend, 'selected_channels', lambda server: {'qq'})
    monkeypatch.setattr('runtime.reply.proactive_runtime.enabled', lambda: False)
    world._state_root = lambda: tmp_path
    return {'service': Service(), 'status': {'qq': status}}, calls


@pytest.mark.parametrize('qq,letters,starts', [(6, 1, True), (1, 6, False)])
def test_the_scheduler_leaves_the_slot_and_the_opportunity_alone_when_letters_are_preferred(
        monkeypatch, tmp_path, qq, letters, starts):
    world = owner_world(qq=many(qq_message, qq), letters=many(letter, letters))
    runtime, calls = contact_world(monkeypatch, tmp_path, world)
    initiative = initiative_for(world)
    asyncio.run(backend._proactive_contact(world, runtime, initiative))
    assert len(calls) == (1 if starts else 0)
    assert world._jev_contact_channel is None
    assert (initiative.due != NOW - 1) is starts  # Not attempted, so still due when the preference changes.


def test_qq_takes_over_when_letters_are_preferred_but_switched_off(monkeypatch, tmp_path):
    world = owner_world(qq=many(qq_message, 1), letters=many(letter, 6), letters_on=False)
    runtime, calls = contact_world(monkeypatch, tmp_path, world)
    asyncio.run(backend._proactive_contact(world, runtime, initiative_for(world)))
    assert len(calls) == 1


def letter_server(monkeypatch, tmp_path, world, *, qq_ready):
    import local_server as server
    monkeypatch.setattr(server, 'store', world.store)
    monkeypatch.setattr(server, '_state_root', lambda: tmp_path)
    monkeypatch.setattr(server, '_proactive_settings', world._proactive_settings)
    monkeypatch.setattr(server, '_proactive_ready', lambda: True)
    monkeypatch.setattr(server, '_qq_contact_ready', (lambda: qq_ready) if qq_ready is not None else None)
    monkeypatch.setattr(server, '_proactive_reason', 'waiting')
    monkeypatch.setattr(server.time, 'time', lambda: NOW)
    observed = []
    monkeypatch.setattr(server, '_observe_letter_initiative', observed.append)
    return server, observed


@pytest.mark.parametrize('qq,letters,qq_ready,letters_on,open_', [
    (6, 1, True, True, False), (1, 6, True, True, True), (2, 2, True, True, False), (0, 0, True, True, False),
    (6, 1, False, True, True), (0, 0, False, True, True), (6, 1, None, True, True), (1, 6, True, False, False),
])
def test_letters_start_contact_only_where_the_owner_writes_most_or_qq_cannot(
        monkeypatch, tmp_path, qq, letters, qq_ready, letters_on, open_):
    world = owner_world(qq=many(qq_message, qq), letters=many(letter, letters), letters_on=letters_on)
    server, _ = letter_server(monkeypatch, tmp_path, world, qq_ready=qq_ready)
    assert server._letter_channel_open() is open_


@pytest.mark.parametrize('jev', [False, True])
@pytest.mark.parametrize('qq,letters,passes', [(6, 1, False), (1, 6, True)])
def test_the_letter_tick_stops_before_any_paid_step_when_qq_is_preferred(monkeypatch, tmp_path, jev, qq, letters, passes):
    world = owner_world(qq=many(qq_message, qq), letters=many(letter, letters))
    server, observed = letter_server(monkeypatch, tmp_path, world, qq_ready=True)
    if jev:
        monkeypatch.setenv('OLIVIA_JEV_DECISION_URL', 'http://127.0.0.1:8097/v1/companion/decide')
    else:
        monkeypatch.delenv('OLIVIA_JEV_DECISION_URL', raising=False)
    async def paid(*args, **kwargs):
        raise AssertionError('a paid step ran for the non-preferred channel')
    monkeypatch.setattr(server, '_prepare_proactive_turn', paid)
    asyncio.run(server._proactive_tick())
    # With no pending opportunity the open gate gets as far as "nothing to say"; the closed one stops earlier.
    assert server._proactive_reason == ('no_opportunity' if passes else 'waiting')
    assert ('deferred' in observed) is (not passes)
    assert getattr(server, '_jev_contact_channel', None) is None


def test_a_publication_is_rechecked_against_the_preference_but_a_gift_answer_is_not(monkeypatch, tmp_path):
    world = owner_world(qq=many(qq_message, 6), letters=many(letter, 1))
    server, observed = letter_server(monkeypatch, tmp_path, world, qq_ready=True)
    monkeypatch.setattr(server, '_active_undelivered_letter', lambda **kwargs: None)
    monkeypatch.setattr(server, '_refresh_proactive_context',
                        lambda **kwargs: {'blocked': False, 'unread': False, 'remaining': 3, 'candidates': []})
    from runtime.personal_chat import contact_activity
    monkeypatch.setattr(contact_activity, 'cross_channel_reason', lambda *a, **k: None)
    intent = {'id': 'x' * 32, 'kind': 'life_share', 'source_id': 'reply:a:1'}
    assert server._proactive_opportunity_current(intent) is False and 'deferred' in observed
    gift = {'id': 'y' * 32, 'kind': 'gift', 'gift_id': 'gift:1', 'brief': '谢谢', 'photo': False, 'source_id': 'gift:first'}
    assert server._proactive_opportunity_current(gift) is True


def test_the_chat_backend_publishes_whether_qq_can_start_contact_and_withdraws_it(tmp_path, monkeypatch):
    import local_server
    from aiohttp import web
    from runtime.personal_chat import qq
    monkeypatch.delenv('OLIVIA_PERSONAL_CHAT_CONFIG', raising=False)
    monkeypatch.setenv('OLIVIA_PERSONAL_QQ_TOKEN', 'synthetic-token-123456789')
    folder = tmp_path / 'personal-chat'
    folder.mkdir()
    (folder / 'config.json').write_text(json.dumps({'qq': {'url': 'ws://127.0.0.1:3001', 'account': '100', 'owner': '200'}}),
                                        encoding='utf-8')
    (folder / 'setup-choice.json').write_text(json.dumps({'channels': ['qq']}), encoding='utf-8')
    seen, voice = [], [True]
    async def listen_qq(url, token, account, owner, handler, stop, **kwargs):
        state = kwargs.get('state_callback', lambda _state: None)
        state('CONNECTED')
        seen.append(server._qq_contact_ready())  # Connected, but nobody has written yet: no target to start from.
        sender = SimpleNamespace(is_available=lambda: True, audio=lambda path: None)
        handler.ready('qq', sender)
        seen.append(server._qq_contact_ready())
        voice[0] = False  # No voice configured: QQ cannot start her own contact.
        seen.append(server._qq_contact_ready())
        voice[0] = True
        sender.is_available = lambda: False
        seen.append(server._qq_contact_ready())
        sender.is_available = lambda: True
        state('RECONNECTING')
        seen.append(server._qq_contact_ready())
        await stop.wait()
    monkeypatch.setattr(qq, 'run_qq', listen_qq)
    server = SimpleNamespace(store=local_server.Store(), _state_root=lambda: tmp_path,
        _require_store_state_available=lambda: None, _persist_store_state=lambda: None,
        _safe_log=lambda *a, **k: None, _atomic_write_store_file=local_server._atomic_write_store_file,
        _voice_reply_configured=lambda _: voice[0])
    async def run_once():
        app = web.Application()
        backend.install_personal_chat(app, server)
        runner = web.AppRunner(app)
        await runner.setup()
        await asyncio.sleep(.05)
        assert callable(server._qq_contact_ready)
        await runner.cleanup()
    asyncio.run(run_once())
    assert seen == [False, True, False, False, False]
    assert server._qq_contact_ready is None
