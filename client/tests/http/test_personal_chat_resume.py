"""A reply cut off by a restart is written again once, never sent twice, and never for an old or answered message."""
import asyncio
import json
import time
from contextlib import suppress
from types import SimpleNamespace

import pytest

from runtime.personal_chat import backend
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService, RESUME_WINDOW_SECONDS

BINDINGS = {'qq': ('10000', '20000'), 'wechat': ('wx', 'owner')}


def message(channel, message_id='1', text='hello'):
    account, owner = BINDINGS[channel]
    return PersonalMessage(channel, account, owner, message_id, text)


async def never(*args):
    raise AssertionError('nothing may be generated, sent or committed here')


async def stopped_mid_reply(channel, how, rows=None):
    """The store as a restart leaves it: she was writing the reply to a fresh message.

    'killed' is a process that disappeared (the row is still GENERATING); 'cancelled' is an orderly shutdown
    that cancelled the generation (the row was marked failed on the way out)."""
    rows = [] if rows is None else rows
    started = asyncio.Event()

    async def generate(event, row):
        started.set()
        await asyncio.Event().wait()
    service = PersonalChatService(rows, lambda: None, generate, never, BINDINGS)
    task = asyncio.create_task(service.handle(message(channel), never))
    await asyncio.wait_for(started.wait(), 1)
    if how == 'killed':
        snapshot = json.loads(json.dumps(rows))
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    if how == 'cancelled':
        snapshot = json.loads(json.dumps(rows))
    return snapshot


def after_restart(rows, generated=None, sent=None, committed=None, persisted=None):
    async def generate(event, row):
        generated.append(event.text)
        return 'reply to ' + event.text

    async def commit(row):
        committed.append(row['letter_id'])
    return PersonalChatService(rows, lambda: persisted.append(1), generate, commit, BINDINGS)


def age(row, seconds):
    row['created_at'] = time.time() - seconds
    return row


def shown_failure(rows, channel):
    return backend.reply_failures(SimpleNamespace(store=SimpleNamespace(personal_chats=rows)),
                                  {'status': {channel: 'CONNECTED'}, 'errors': {}})[0]


@pytest.mark.parametrize('channel', ['qq', 'wechat'])
@pytest.mark.parametrize('how', ['killed', 'cancelled'])
def test_a_recent_interrupted_reply_is_written_again_once_and_the_notice_goes_away(channel, how):
    async def scenario():
        snapshot = await stopped_mid_reply(channel, how)
        row = age(snapshot[0], 120)
        generated, sent, committed, persisted = [], [], [], []
        service = after_restart(snapshot, generated, sent, committed, persisted)
        if how == 'cancelled':
            assert shown_failure(snapshot, channel) == {channel: 'PERSONAL_CHAT_GENERATION_INTERRUPTED'}
        queued = await service.recover()
        assert [event.exchange_id for event in queued] == [message(channel).exchange_id]
        assert row['delivery_status'] == 'RECEIVED' and row['letter_status'] == 'PROCESSING'
        assert 'error_code' not in row and 'generation_retryable' not in row and 'generation_recovery' not in row
        assert row['generation_resumed_at'] > 0 and persisted and not generated and not sent
        assert shown_failure(snapshot, channel) == {}
        if channel == 'qq':  # the connection takes it from the durable backlog
            assert [event.exchange_id for event in service.pending('qq')] == [queued[0].exchange_id]

        async def send(text):
            sent.append(text)
            return 'platform-id'
        await service.handle(queued[0], send)
        await asyncio.gather(*service.consumer_tasks.values())
        assert generated == ['hello'] and sent == ['reply to hello'] and committed == [row['letter_id']]
        assert row['delivery_status'] == 'DELIVERED' and row['generation_attempts'] == 1
        assert shown_failure(snapshot, channel) == {}
        assert await service.recover() == () and generated == ['hello'] and sent == ['reply to hello']
    asyncio.run(scenario())


@pytest.mark.parametrize('how', ['killed', 'cancelled'])
def test_a_reply_that_is_cut_off_a_second_time_is_not_written_again(how):
    async def scenario():
        first = await stopped_mid_reply('qq', how)
        age(first[0], 60)
        queued = await after_restart(first, [], [], [], []).recover()
        assert len(queued) == 1 and first[0]['generation_resumed_at']
        # The second run is stopped in the middle of the resumed reply as well.
        second = await stopped_mid_reply('qq', how, rows=first)
        row = second[0]
        assert row['generation_resumed_at'] and row['delivery_status'] == ('GENERATING' if how == 'killed' else 'FAILED')
        generated, persisted = [], []
        assert await after_restart(second, generated, [], [], persisted).recover() == ()
        assert row['delivery_status'] == 'FAILED' and row['error_code'] == 'PERSONAL_CHAT_GENERATION_INTERRUPTED'
        assert row['generation_interrupted'] is True and not generated
        if how == 'killed':
            assert row['generation_retryable'] is False and row['generation_recovery'] == 'NO_CANDIDATE'
        assert shown_failure(second, 'qq') == {'qq': 'PERSONAL_CHAT_GENERATION_INTERRUPTED'}
    asyncio.run(scenario())


@pytest.mark.parametrize('how', ['killed', 'cancelled'])
def test_a_message_older_than_fifteen_minutes_keeps_the_failed_notice(how):
    assert RESUME_WINDOW_SECONDS == 15 * 60

    async def scenario():
        for seconds, resumed in ((RESUME_WINDOW_SECONDS - 30, True), (RESUME_WINDOW_SECONDS + 30, False)):
            snapshot = await stopped_mid_reply('qq', how)
            row = age(snapshot[0], seconds)
            queued = await after_restart(snapshot, [], [], [], []).recover()
            assert bool(queued) is resumed
            if resumed:
                assert row['delivery_status'] == 'RECEIVED'
            else:
                assert row['delivery_status'] == 'FAILED' and row['error_code'] == 'PERSONAL_CHAT_GENERATION_INTERRUPTED'
                assert row['generation_interrupted'] is True and 'generation_resumed_at' not in row
                assert shown_failure(snapshot, 'qq') == {'qq': 'PERSONAL_CHAT_GENERATION_INTERRUPTED'}
                if how == 'killed':
                    assert row['generation_retryable'] is False
    asyncio.run(scenario())


@pytest.mark.parametrize('how', ['killed', 'cancelled'])
def test_a_message_that_a_newer_answered_message_replaced_keeps_the_failed_notice(how):
    async def scenario():
        snapshot = await stopped_mid_reply('qq', how)
        row = age(snapshot[0], 300)
        generated, sent = [], []
        service = after_restart(snapshot, generated, sent, [], [])

        async def send(text):
            sent.append(text)
            return 'platform-id'
        # Typed and answered after the lost one (the owner wrote again while the app was starting).
        await service.handle(message('qq', '2', 'are you there'), send)
        assert generated == ['are you there'] and snapshot[1]['delivery_status'] == 'DELIVERED'
        assert await service.recover() == ()
        assert row['delivery_status'] == 'FAILED' and row['error_code'] == 'PERSONAL_CHAT_GENERATION_INTERRUPTED'
        assert 'generation_resumed_at' not in row and generated == ['are you there']
    asyncio.run(scenario())


def test_a_newer_message_that_has_no_reply_yet_does_not_stop_the_resume():
    async def scenario():
        snapshot = await stopped_mid_reply('qq', 'killed')
        row = age(snapshot[0], 90)
        service = after_restart(snapshot, [], [], [], [])
        await service.ingest(message('qq', '2', 'still there?'))
        queued = await service.recover()
        assert [event.exchange_id for event in queued] == [message('qq').exchange_id]
        assert row['delivery_status'] == 'RECEIVED' and snapshot[1]['delivery_status'] == 'RECEIVED'
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['send_reserved', 'other_attempt_reserved', 'receipt', 'proactive', 'unbound_account',
                                    'no_source_message', 'already_resumed'])
def test_a_reply_that_may_have_been_sent_or_is_not_the_owners_is_never_written_again(change):
    async def scenario():
        snapshot = await stopped_mid_reply('qq', 'killed')
        row = age(snapshot[0], 60)
        row['reply_text'] = 'draft'
        if change == 'send_reserved':
            row['send_attempt_id'] = row['generation_attempt_id']
        elif change == 'other_attempt_reserved':
            row['send_attempt_id'] = 'an-earlier-attempt'
        elif change == 'receipt':
            row['delivery_receipt'] = {'attempt_id': 'x', 'confirmed': False}
        elif change == 'proactive':
            row.update(origin='proactive', source_messages={})
        elif change == 'unbound_account':
            row['binding_id'] = 'a-binding-that-is-gone'
        elif change == 'no_source_message':
            row['source_messages'] = {}
        elif change == 'already_resumed':
            row['generation_resumed_at'] = time.time() - 600
        service = after_restart(snapshot, [], [], [], [])
        assert await service.recover() == ()
        if change == 'send_reserved':
            assert row['delivery_status'] == 'DELIVERY_UNCONFIRMED'
            assert row['error_code'] == 'PERSONAL_CHAT_DELIVERY_UNCONFIRMED'
        else:
            assert row['delivery_status'] == 'FAILED' and row['error_code'] == 'PERSONAL_CHAT_GENERATION_INTERRUPTED'
            assert row['generation_retryable'] is False
        assert service.pending('qq') == ()
    asyncio.run(scenario())


def test_a_cancelled_generation_of_the_running_process_is_left_alone():
    async def scenario():
        rows = []
        started = asyncio.Event()

        async def generate(event, row):
            started.set()
            await asyncio.Event().wait()
        service = PersonalChatService(rows, lambda: None, generate, never, BINDINGS)
        task = asyncio.create_task(service.handle(message('qq'), never))
        await asyncio.wait_for(started.wait(), 1)
        assert service.replies_in_flight()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert not service.replies_in_flight()
        assert rows[0]['delivery_status'] == 'FAILED'
        assert await service.recover() == ()
        assert rows[0]['delivery_status'] == 'FAILED' and 'generation_resumed_at' not in rows[0]
    asyncio.run(scenario())


def test_a_reply_is_in_flight_from_the_moment_it_is_handled_until_it_is_done():
    async def scenario():
        rows, seen = [], []
        release = asyncio.Event()

        async def generate(event, row):
            seen.append(service.replies_in_flight())
            await release.wait()
            return 'reply'

        async def send(text):
            seen.append(service.replies_in_flight())
            return 'platform-id'

        async def commit(row):
            pass
        service = PersonalChatService(rows, lambda: None, generate, commit, BINDINGS)
        assert not service.replies_in_flight()
        await service.ingest(message('qq'))   # received and saved, but nothing is being written yet
        assert not service.replies_in_flight()
        task = asyncio.create_task(service.handle(message('qq'), send))
        await asyncio.sleep(0.05)
        assert service.replies_in_flight()
        release.set()
        await task
        assert seen == [True, True] and not service.replies_in_flight()
        # A failed reply does not leave it stuck on.
        service.generate = never
        rows[0].update(delivery_status='RECEIVED', generation_attempts=0)
        with pytest.raises(AssertionError):
            await service.handle(message('qq'), send)
        assert not service.replies_in_flight()
    asyncio.run(scenario())


def test_resumed_qq_replies_are_started_at_once_only_when_qq_is_connected():
    async def scenario():
        started = []

        class Runner:
            def __init__(self, sender):
                self.sender = sender
                self.queued = [message('qq', '1'), message('qq', '2')]

            def pending(self, channel):
                assert channel == 'qq'
                return tuple(self.queued)

            async def __call__(self, event, sender):
                started.append((event.message_id, sender))

        live = SimpleNamespace(is_available=lambda: True)
        down = SimpleNamespace(is_available=lambda: False)
        resumed = (message('qq', '1'), message('wechat', '1'))
        for sender in (None, down):                       # the connection will pick it up when it connects
            backend._resume_now({'qq_runner': Runner(sender)}, resumed)
        backend._resume_now({'qq_runner': Runner(live)}, ())
        backend._resume_now({}, resumed)
        await asyncio.sleep(0)
        assert started == []
        runtime = {'qq_runner': Runner(live)}
        backend._resume_now(runtime, resumed)             # only the resumed QQ message, not the other pending one
        await asyncio.sleep(0.01)
        assert started == [('1', live)] and not runtime['resume_tasks']
    asyncio.run(scenario())


def test_the_recovery_loop_hands_resumed_replies_to_a_connected_qq(monkeypatch):
    async def scenario():
        snapshot = await stopped_mid_reply('qq', 'killed')
        age(snapshot[0], 30)
        generated, sent = [], []
        service = after_restart(snapshot, generated, sent, [], [])
        stop = asyncio.Event()
        live = SimpleNamespace(is_available=lambda: True)

        class Runner:
            sender = live

            def pending(self, channel):
                return service.pending(channel)

            async def __call__(self, event, sender):
                async def send(text):
                    sent.append(text)
                    return 'platform-id'
                await service.handle(event, send)
                stop.set()
        runtime = {'stop': stop, 'service': service, 'status': {'qq': 'CONNECTED'}, 'errors': {}, 'qq_runner': Runner()}
        server = SimpleNamespace(store=SimpleNamespace(personal_chats=snapshot), _state_root=lambda: None,
                                 _safe_log=lambda *args, **kwargs: None)
        monkeypatch.setattr(backend, '_publish_status', lambda *args: None)
        await asyncio.wait_for(backend._recover_chat_loop(server, runtime, interval_seconds=5), 3)
        await asyncio.gather(*service.consumer_tasks.values())
        assert generated == ['hello'] and sent == ['reply to hello'] and snapshot[0]['delivery_status'] == 'DELIVERED'
        assert shown_failure(snapshot, 'qq') == {} and runtime['status']['recovery'] == 'READY'
    asyncio.run(scenario())
