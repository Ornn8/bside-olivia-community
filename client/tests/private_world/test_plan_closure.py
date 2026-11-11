import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from runtime.private_world import plan_closure
from runtime.private_world.daily_life import DailyLifeStore

NOW = datetime(2026, 11, 5, 9, tzinfo=timezone.utc)
LINE = "I'll send the photo. Fair warning: it's not dramatic. A shuttered shop, glass, a blur of a woman with an orange bag,"


def _item(item_id, quote=LINE, *, status='planned', kind='shared', actor='linli'):
    return dict(id=item_id, title=quote[:60], detail=quote, quote=quote, status=status, kind=kind, actor=actor)


class Port:
    """Answers each item question by a rule on the item's own words, and records what was asked."""

    def __init__(self, delivered=lambda item: True):
        self.calls, self.delivered = [], delivered

    async def ask(self, state, questions, *, purpose):
        self.calls.append((state, questions, purpose))
        return {key: 'delivered' if self.delivered(state['items'][key]) else 'open' for key in questions}


class Server:
    def __init__(self, tmp_path):
        self.persisted = 0
        self.logged = []
        self.store = SimpleNamespace(letters=[], personal_chats=[])
        self.daily_life_runtime = SimpleNamespace(store=DailyLifeStore(tmp_path / 'life.db'))

    def _persist_store_state(self):
        self.persisted += 1

    def _safe_log(self, name, **_):
        self.logged.append(name)

    def say(self, letter_id, items, *, reply=LINE, user='Send me a photo?', at=NOW, delivered=True, channel='qq', **fields):
        """A chat row whose reply was recorded in the world, then (optionally) its photo was delivered."""
        self.daily_life_runtime.store.record_exchange(f'reply:{letter_id}:1', user, reply, items, occurred_at=at)
        row = dict(letter_id=letter_id, reply_revision=1, reply_text=reply, channel=channel,
                   daily_life_status='COMMITTED', created_at=at.timestamp(), **fields)
        if delivered:
            row['image_delivery_status'] = 'DELIVERED'
        (self.store.personal_chats if channel == 'qq' else self.store.letters).append(row)
        return row

    def states(self):
        state = self.daily_life_runtime.store.exchange_state(now=datetime.now(timezone.utc) + timedelta(minutes=1))
        return {p['id']: p['status'] for p in (*state['projects'], *state['shared'])}


def run(coro):
    return asyncio.run(coro)


async def _settled():
    await asyncio.gather(*[task for task in plan_closure._tasks if not task.done()])


def test_photo_sent_after_the_line_leaves_the_promise_planned_without_closure(tmp_path, monkeypatch):
    """The failure on main: the extraction only read the line, and nothing ever links the picture to it."""
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: None)
    server = Server(tmp_path)
    server.say('L1', [_item('send-photo')])
    assert server.states() == {'send-photo': 'planned'}
    from runtime.image_understanding import commit_image_memory
    run(commit_image_memory(server, server.store.personal_chats[0]))
    assert server.states() == {'send-photo': 'planned'}  # an unrelated consumer cannot close it


def test_photo_acknowledgement_closes_the_promise_in_the_background(tmp_path, monkeypatch):
    port = Port()
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: port)
    server = Server(tmp_path)
    row = server.say('L1', [_item('send-photo')], channel='letter')

    async def acknowledged():
        from runtime.image_understanding import commit_image_memory
        await commit_image_memory(server, row)
        assert port.calls == []  # the acknowledgement itself does not wait for the question
        await _settled()

    run(acknowledged())
    assert server.states() == {'send-photo': 'completed'} and len(port.calls) == 1


def test_delivered_photo_completes_the_promise_of_its_own_reply(tmp_path):
    server = Server(tmp_path)
    row = server.say('L1', [_item('send-photo')])
    port = Port()
    assert run(plan_closure.settle(server, row, port=port)) is True
    assert server.states() == {'send-photo': 'completed'}
    assert [call[2] for call in port.calls] == ['plan-item-delivery']
    state, questions, _ = port.calls[0]
    assert state['delivered'] == [{'kind': '照片'}] and state['items']['i0']['her_words'] == LINE
    assert set(questions['i0']['criteria']['delivered']) == {'what', 'not_for', 'examples'}
    # Asking again changes nothing and costs nothing.
    assert run(plan_closure.settle(server, row, port=port)) is False
    assert len(port.calls) == 1 and row['plan_closure'] == 'photo'
    # Her own words stay as they were said; the closure is a separate event.
    with server.daily_life_runtime.store._db() as db:
        moments = [row[0] for row in db.execute('SELECT source_id FROM life_moments')]
    assert 'reply:L1:1' in moments and any(name.startswith('reply:L1:1.closure.') for name in moments)


def test_only_the_items_the_delivery_fulfils_are_closed(tmp_path):
    server = Server(tmp_path)
    coffee = "I'll bring you a coffee when we meet."
    row = server.say('L1', [_item('send-photo'), _item('coffee', coffee)], reply=LINE + ' ' + coffee)
    port = Port(delivered=lambda item: 'photo' in item['her_words'])
    run(plan_closure.settle(server, row, port=port))
    assert server.states() == {'send-photo': 'completed', 'coffee': 'planned'}


def test_items_of_other_replies_and_the_users_own_items_are_not_candidates(tmp_path):
    server = Server(tmp_path)
    server.say('OLD', [_item('older-photo')], delivered=False)
    row = server.say('L2', [_item('mine', "I'll look at it tonight.", actor='user'),
                            _item('asked', 'Could you send me yours?', status='awaiting_user')],
                     user="Send me a photo? I'll look at it tonight.", reply='Could you send me yours?')
    port = Port()
    run(plan_closure.settle(server, row, port=port))
    assert port.calls == []  # nothing of hers is open for this reply: no question is asked
    assert row['plan_closure'] == 'photo'
    assert server.states() == {'older-photo': 'planned', 'mine': 'planned', 'asked': 'awaiting_user'}


def test_nothing_is_asked_or_closed_until_something_was_confirmed_delivered(tmp_path):
    server = Server(tmp_path)
    row = server.say('L1', [_item('send-photo')], delivered=False)
    row['image_delivery_status'] = 'SENDING'
    port = Port()
    assert run(plan_closure.settle(server, row, port=port)) is False
    assert port.calls == [] and server.states() == {'send-photo': 'planned'}


def test_line_read_aloud_is_not_a_delivery(tmp_path):
    server = Server(tmp_path)
    row = server.say('L1', [_item('send-photo')], delivered=False)
    from runtime.reply.media_delivery import make_delivery
    sha = hashlib.sha256(b'x').hexdigest()
    base = dict(letter_id='L', reply_revision=1, reply_text='x', letter_status='COMPLETED', private_world_reply_sha256=sha)
    assert plan_closure.delivered_kinds(row) == []
    speech = dict(base, media_deliveries=[make_delivery(base, component='speech', presentation='audio', occurred_at=NOW)])
    video = dict(base, media_deliveries=[make_delivery(base, component='speech', presentation='video', occurred_at=NOW)])
    song = dict(base, media_deliveries=[make_delivery(base, component='music', presentation='audio', occurred_at=NOW)])
    cover = dict(base, media_deliveries=[make_delivery(base, component='cover', presentation='audio', occurred_at=NOW)])
    assert plan_closure.delivered_kinds(speech) == []
    assert plan_closure.delivered_kinds(video) == ['video']
    assert plan_closure.delivered_kinds(song) == ['song']
    assert plan_closure.delivered_kinds(cover) == ['cover']
    assert plan_closure.delivered_kinds({'cover_status': 'DELIVERED', 'piano_delivery_status': 'DELIVERED',
                                         'image_delivery_status': 'DELIVERED'}) == ['cover', 'photo', 'piano']


def test_a_failed_question_leaves_the_item_open_and_is_tried_at_most_three_times(tmp_path):
    server = Server(tmp_path)
    row = server.say('L1', [_item('send-photo')])

    class Broken:
        calls = 0

        async def ask(self, state, questions, *, purpose):
            Broken.calls += 1
            raise ValueError('JEV_RESPONSE_INVALID')

    for _ in range(5):
        with pytest.raises(ValueError):
            run(plan_closure.settle(server, row, port=Broken()))
        if row['plan_closure_attempts'] >= plan_closure.MAX_ATTEMPTS:
            break
    assert Broken.calls == 3 and server.states() == {'send-photo': 'planned'}
    assert run(plan_closure.settle(server, row, port=Port())) is False  # the budget is spent; no fourth question


def test_an_answer_that_is_not_a_choice_closes_nothing(tmp_path):
    server = Server(tmp_path)
    row = server.say('L1', [_item('send-photo')])

    class Odd:
        async def ask(self, state, questions, *, purpose):
            return {key: 'yes' for key in questions}

    with pytest.raises(ValueError, match='JEV_RESPONSE_INVALID'):
        run(plan_closure.settle(server, row, port=Odd()))
    assert server.states() == {'send-photo': 'planned'}


def test_a_closed_promise_stays_closed_when_the_same_identity_is_planned_again(tmp_path):
    server = Server(tmp_path)
    row = server.say('L1', [_item('send-photo')])
    run(plan_closure.settle(server, row, port=Port()))
    store = server.daily_life_runtime.store
    store.record_exchange('reply:L9:1', 'again?', 'Sure.', [_item('send-photo', 'Sure.')], occurred_at=NOW + timedelta(minutes=30))
    assert server.states() == {'send-photo': 'completed'}


def test_delivery_seen_before_the_promise_was_recorded_is_settled_once_it_is(tmp_path, monkeypatch):
    server = Server(tmp_path)
    row = server.say('L1', [_item('send-photo')])
    row['daily_life_status'] = 'PENDING'
    port = Port()
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: port)
    assert run(plan_closure.settle(server, row)) is False and port.calls == []
    row['daily_life_status'] = 'COMMITTED'  # the exchange extraction of the same reply finished

    async def after_commit():
        plan_closure.schedule_settle(server, row)
        await _settled()

    run(after_commit())
    assert server.states() == {'send-photo': 'completed'} and len(port.calls) == 1


def test_backfill_closes_newest_first_and_a_second_run_finishes_the_rest(tmp_path, monkeypatch):
    server = Server(tmp_path)
    for n in range(30):
        server.say(f'D{n}', [_item(f'photo-{n}')], at=NOW - timedelta(minutes=n))
    for n in range(5):  # delivered, but she had promised nothing in that reply
        server.say(f'N{n}', [], at=NOW - timedelta(hours=1, minutes=n))
    server.say('U', [_item('undelivered')], delivered=False)
    assert len(plan_closure.pending_rows(server)) == 30  # the dry run: the most questions a full pass would ask
    port = Port()
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: port)
    assert run(plan_closure.backfill(server, limit=25)) == 25
    states = server.states()
    assert sum(status == 'completed' for status in states.values()) == 25
    assert all(states[f'photo-{n}'] == 'completed' for n in range(25)) and states['photo-29'] == 'planned'
    assert run(plan_closure.backfill(server, limit=25)) == 5
    assert {v for k, v in server.states().items() if k != 'undelivered'} == {'completed'}
    assert run(plan_closure.backfill(server, limit=25)) == 0 and len(port.calls) == 30


def test_backfill_asks_nothing_when_no_delivered_reply_holds_an_open_item(tmp_path, monkeypatch):
    server = Server(tmp_path)
    server.say('A', [], delivered=True)
    server.say('B', [_item('x')], delivered=False)
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: pytest.fail('no question expected'))
    assert plan_closure.pending_rows(server) == []
    assert run(plan_closure.backfill(server)) == 0


def test_the_closure_event_goes_with_its_letter_when_that_letter_is_deleted(tmp_path):
    from runtime.imports.letter_delete import Sources, sql_plan
    server = Server(tmp_path)
    row = server.say('L1', [_item('send-photo')])
    run(plan_closure.settle(server, row, port=Port()))
    patches = sql_plan(server.daily_life_runtime.store.path, Sources({'letter_id': 'L1', 'reply_revision': 1}, 'reply'))
    removed = [patch['before']['source_id'] for patch in patches if patch['table'] == 'life_moments' and patch['after'] is None]
    assert any(name.startswith('reply:L1:1.closure.') for name in removed)
