"""The emotion ledger must agree with the reference replay and stop growing with history.

`_state_reference` is the whole-list replay the store used to run on every read:
quadratic in the number of appraisals, and read about seventeen times per reply.
The ledger replays each stored row once and syncs to the rows on every use.
"""
from datetime import datetime, timedelta, timezone
import random
import sqlite3
import time

import pytest

from runtime.private_world import character_emotion as module
from runtime.private_world.character_emotion import CharacterEmotionStore, _Projection, _ledger_for
from runtime.private_world.daily_life import DailyLifeStore


BASE = datetime(2026, 9, 27, 2, tzinfo=timezone.utc)


def stamp(minutes):
    return module._time(BASE + timedelta(minutes=minutes))


def insert(db, source_id, minutes, *, reaction='none', concern=None, revises=None, dependencies=None, text=None):
    """Write one source and its appraisal with the store's own hashes; dependencies are the caller's."""
    text = text or f'{source_id}：今天过得怎么样？'
    source = module._source(source_id, 'received_input', text, stamp(minutes))
    order = db.execute('INSERT INTO character_emotion_sources VALUES (?,?,?)',
                       (source_id, source['occurred_at'], module._json(source))).lastrowid
    item = dict(source_id=source_id, quote=text[:3] if (reaction != 'none' or concern or revises) else '',
                reported_affect=None, reaction=reaction, goal_or_need=None, action_tendency='none',
                concern=concern, revises=revises)
    dependencies = dependencies or {}
    version = module._hash({'source': {**source, 'source_order': order}, 'appraisal': item, 'dependencies': dependencies})
    basis = dict(context_version='x', as_of=source['occurred_at'], source_hash=source['source_hash'],
                 dependencies=dependencies)
    db.execute('INSERT INTO character_emotion_appraisals VALUES (?,?,?,?)',
               (source_id, module._json(item), version, module._json(basis)))
    return {**item, 'occurred_at': source['occurred_at'], 'source_order': order, 'version': version,
            '_dependencies': dependencies}


def seed(path, count, *, concern_every=40):
    """Valid appraised received sources, oldest first, as a long conversation leaves them."""
    CharacterEmotionStore(path)
    projection, opened = _Projection(), None
    with sqlite3.connect(path) as db:
        for index in range(count):
            when = BASE + timedelta(minutes=7 * index)
            # The read context is what the projection says just before this record.
            context_hash = module._hash(projection.context(module._time(when)))
            dependencies, concern = {'read_context': context_hash}, None
            if index and index % concern_every == 0 and opened is None:
                opened = f'emotion:{index}'
                concern = dict(id=opened, action='open', summary='有点担心')
            elif opened and index % concern_every == concern_every // 2:
                concern = dict(id=opened, action='resolve', summary='有点担心')
                dependencies['concern_version'] = projection.concern_version(opened)
                opened = None
            record = insert(db, f'received-user:qq:{index:08d}', 7 * index,
                            reaction='pleased' if index % 3 == 0 else 'none', concern=concern, dependencies=dependencies)
            projection.add(record, datetime.fromisoformat(record['occurred_at']))
    return BASE + timedelta(minutes=7 * count)


def assert_agrees(db, at, before=None, context_at=None):
    context_at = context_at or at
    live, concerns, stale = CharacterEmotionStore._state_reference(db, at, before=before)
    view = CharacterEmotionStore._state_view(db, at, before=before, context_at=context_at)
    assert view.live == live
    assert dict(view.concerns) == concerns
    assert list(view.concerns.values()) == sorted(concerns.values(), key=lambda c: (c['occurred_at'], c['source_order']))
    assert view.stale == stale
    assert view.context == CharacterEmotionStore._context(live, concerns, context_at)


# --- agreement with the reference replay -------------------------------------------------

def appraisal(rng, source, context):
    reaction = rng.choice(['pleased', 'calm', 'concerned', 'none', 'none'])
    open_ids = [c['id'] for c in context['concerns'] if c['status'] == 'open']
    changes, roll = [], rng.random()
    if roll < 0.25:
        changes.append(dict(id='c' + str(rng.randint(0, 3)), action='open', summary='牵挂'))
    elif roll < 0.5 and open_ids:
        changes.append(dict(id=rng.choice(open_ids), action='resolve', summary='牵挂'))
    elif roll < 0.55:
        changes.append(dict(id='c9', action='resolve', summary='牵挂'))  # nothing is open: refused
    concern = changes[0] if len(changes) == 1 else changes or None
    revises = None
    if rng.random() < 0.25 and context['prior_appraisals']:
        revises = dict(source_id=rng.choice(context['prior_appraisals'])['source_id'], action='withdraw')
    meaningful = reaction != 'none' or concern or revises
    return dict(source_id=source['source_id'], quote=source['text'][:3] if meaningful else '', reported_affect=None,
                reaction=reaction, goal_or_need=None, action_tendency='none', concern=concern, revises=revises)


@pytest.mark.parametrize('seed_value', range(12))
def test_ledger_agrees_with_reference_replay_through_late_arrivals_withdrawals_and_stale_rows(tmp_path, seed_value):
    rng = random.Random(seed_value)
    path = tmp_path / 'daily_life.sqlite3'
    DailyLifeStore(path)
    store, received = CharacterEmotionStore(path), 0
    for _ in range(30):
        if rng.random() < 0.4 and received < 40:
            for _ in range(rng.randint(1, 3)):  # often earlier than what is already appraised
                minutes = rng.randint(0, 1800) if rng.random() < 0.7 else received * 20
                store.receive(f's{received}', f'第{received}句话', occurred_at=BASE + timedelta(minutes=minutes))
                received += 1
        else:
            now = BASE + timedelta(minutes=rng.randint(0, 2400))
            pending = store.pending_source_ids(before=now, limit=rng.randint(1, 9))
            if pending:
                basis = store.assessment(pending[:rng.randint(1, 4)], now=now)
                items = [appraisal(rng, s, basis['source_contexts'][s['source_id']]) for s in basis['sources']]
                try:
                    store.commit(basis, {'appraisals': items})
                except ValueError:
                    pass  # A refused batch writes nothing; the state below must still agree.
        with store._db() as db:
            for _ in range(3):
                at = stamp(rng.randint(0, 2400))
                before = (at, rng.randint(0, 60)) if rng.random() < 0.5 else None
                assert_agrees(db, at, before, stamp(rng.randint(0, 2400)))
            ledger = _ledger_for(db)
            assert ledger.refused is None and ledger.fingerprint is not None  # not the fallback


def test_hand_written_chains_cycles_and_forward_references_agree_with_the_reference(tmp_path):
    path = tmp_path / 'daily_life.sqlite3'
    DailyLifeStore(path)
    CharacterEmotionStore(path)
    withdraw = lambda target: dict(source_id=target, action='withdraw')
    with sqlite3.connect(path) as db:
        insert(db, 'a', 0, reaction='pleased')
        insert(db, 'b', 10, reaction='sad', revises=withdraw('a'))          # withdraws a
        insert(db, 'c', 20, reaction='calm', revises=withdraw('b'))         # restores a
        insert(db, 'd', 30, reaction='sad', revises=withdraw('later'))      # target not yet accepted
        insert(db, 'later', 40, reaction='pleased')                         # arrives after its reviser
    with sqlite3.connect(path) as db:
        for minutes in (0, 10, 20, 30, 40, 50):
            assert_agrees(db, stamp(minutes))
        assert _ledger_for(db).refused is None
    cycle = tmp_path / 'cycle.sqlite3'
    DailyLifeStore(cycle)
    CharacterEmotionStore(cycle)
    with sqlite3.connect(cycle) as db:
        insert(db, 'x', 0, reaction='pleased', revises=withdraw('y'))
        insert(db, 'y', 10, reaction='sad', revises=withdraw('x'))
    with sqlite3.connect(cycle) as db:
        assert_agrees(db, stamp(5))  # only x: no cycle yet
        for state in (CharacterEmotionStore._state_reference, CharacterEmotionStore._state):
            with pytest.raises(ValueError, match='EMOTION_STORED_EVIDENCE_INVALID'):
                state(db, stamp(20))


def outcome(call):
    try:
        return ('ok', call())
    except ValueError as error:
        return ('refused', str(error))


@pytest.mark.parametrize('seed_value', range(12))
def test_arbitrary_stored_records_agree_with_the_reference_including_forward_references_and_cycles(tmp_path, seed_value):
    """Hand-written rows no commit would produce: unchecked or wrong dependencies, any revision target."""
    rng = random.Random(100 + seed_value)
    path = tmp_path / 'daily_life.sqlite3'
    DailyLifeStore(path)
    CharacterEmotionStore(path)
    names = [f'n{index}' for index in range(18)]
    with sqlite3.connect(path) as db:
        for index, name in enumerate(rng.sample(names, len(names))):
            concern = None
            if rng.random() < 0.4:
                concern = dict(id=f'k{rng.randint(0, 2)}', action=rng.choice(['open', 'open', 'resolve']), summary='牵挂')
            revises = dict(source_id=rng.choice([n for n in names[:5] if n != name]), action='withdraw') if rng.random() < 0.45 else None
            kind = rng.random()
            dependencies = {}
            if kind > 0.5:  # a read context that is right as of the rows so far, or wrong
                prior = CharacterEmotionStore._state_reference(db, stamp(10 * index), before=(stamp(10 * index), 0))
                dependencies = {'read_context': module._hash(CharacterEmotionStore._context(prior[0], prior[1], stamp(10 * index)))
                                if kind < 0.8 else 'wrong'}
            insert(db, name, 10 * rng.randint(0, 20), reaction=rng.choice(['pleased', 'none']), concern=concern,
                   revises=revises, dependencies=dependencies)
    with sqlite3.connect(path) as db:
        for _ in range(25):
            at = stamp(10 * rng.randint(0, 22))
            before = (at, rng.randint(0, 20)) if rng.random() < 0.5 else None
            context_at = stamp(10 * rng.randint(0, 22))
            expected = outcome(lambda: CharacterEmotionStore._state_reference(db, at, before=before))
            got = outcome(lambda: (lambda v: (v.live, dict(v.concerns), v.stale))(
                CharacterEmotionStore._state_view(db, at, before=before, context_at=context_at)))
            assert got == expected
            if expected[0] == 'ok':
                view = CharacterEmotionStore._state_view(db, at, before=before, context_at=context_at)
                assert view.context == CharacterEmotionStore._context(expected[1][0], expected[1][1], context_at)


# --- the ledger syncs to the rows, never to its own memory ------------------------------

def test_ledger_parses_each_stored_row_once_and_follows_every_change(tmp_path, monkeypatch):
    path = tmp_path / 'daily_life.sqlite3'
    DailyLifeStore(path)
    seed(path, 50)
    store = CharacterEmotionStore(path)
    parsed = []
    original = module._parse_row
    monkeypatch.setattr(module, '_parse_row', lambda *args: parsed.append(args[1]) or original(*args))
    now = BASE + timedelta(days=1)
    first = store.view(now=now)
    assert len(parsed) == 50
    for _ in range(5):
        assert store.view(now=now) == first
        store.pending_source_ids(before=now)
    assert len(parsed) == 50  # unchanged rows are not read, parsed or hashed again
    store.receive('received-user:qq:new', '新的一句话', occurred_at=now - timedelta(minutes=1))
    basis = store.assessment(['received-user:qq:new'], now=now)
    store.commit(basis, {'appraisals': [appraisal(random.Random(1), basis['sources'][0], basis['source_contexts'][
        'received-user:qq:new'])]})
    store.view(now=now)
    assert len(parsed) == 51  # only the appended row
    with store._db() as db:
        assert_agrees(db, stamp(1440), None, stamp(1440))


def test_a_rolled_back_batch_leaves_nothing_in_the_ledger(tmp_path):
    path = tmp_path / 'daily_life.sqlite3'
    DailyLifeStore(path)
    store = CharacterEmotionStore(path)
    store.receive('one', '第一句话', occurred_at=BASE)
    store.receive('two', '第二句话', occurred_at=BASE + timedelta(minutes=5))
    now = BASE + timedelta(hours=1)
    basis = store.assessment(['one', 'two'], now=now)
    good = dict(source_id='one', quote='第一句', reported_affect=None, reaction='pleased', goal_or_need=None,
                action_tendency='none', concern=None, revises=None)
    bad = dict(good, source_id='two', quote='第二句', concern=dict(id='nothing', action='resolve', summary='没有这个关注'))
    # The second item resolves a concern that is not open: refused after the first was written.
    with pytest.raises(ValueError, match='EMOTION_APPRAISAL_INVALID'):
        store.commit(basis, {'appraisals': [good, bad]})
    assert store.view(now=now)['reactions'] == []
    with store._db() as db:
        assert_agrees(db, stamp(60))
    assert store.pending_source_ids(before=now) == ['one', 'two']
    store.commit(basis, {'appraisals': [good, dict(good, source_id='two', quote='第二句', reaction='sad')]})
    assert [item['reaction'] for item in store.view(now=now)['reactions']] == ['pleased', 'sad']


def test_changed_stored_text_is_caught_when_the_ledger_rereads_its_rows(tmp_path, monkeypatch):
    path = tmp_path / 'daily_life.sqlite3'
    DailyLifeStore(path)
    seed(path, 20)
    store = CharacterEmotionStore(path)
    store.view(now=BASE + timedelta(days=1))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE character_emotion_appraisals SET payload=replace(payload,'pleased','sad') WHERE rowid=4")
    monkeypatch.setattr(module, '_LEDGER_MAX_AGE', -1.0)  # every use rereads the rows
    with pytest.raises(ValueError, match='EMOTION_STORED_EVIDENCE_INVALID'):
        store.view(now=BASE + timedelta(days=1))


def test_unchanged_text_passes_the_periodic_reread_without_parsing(tmp_path, monkeypatch):
    path = tmp_path / 'daily_life.sqlite3'
    DailyLifeStore(path)
    seed(path, 20)
    store = CharacterEmotionStore(path)
    now = BASE + timedelta(days=1)
    expected = store.view(now=now)
    parsed = []
    original = module._parse_row
    monkeypatch.setattr(module, '_parse_row', lambda *args: parsed.append(1) or original(*args))
    monkeypatch.setattr(module, '_LEDGER_MAX_AGE', -1.0)
    assert store.view(now=now) == expected and parsed == []


# --- per-read work does not grow with the number of appraisals --------------------------

def one_reply(store, now):
    new = 'received-user:qq:new'
    store.receive(new, '新的一句话', occurred_at=now - timedelta(minutes=1))
    basis = store.assessment([new], now=now)
    item = dict(source_id=new, quote='新的一', reported_affect=None, reaction='pleased', goal_or_need=None,
                action_tendency='none', concern=None, revises=None)
    assert store.commit(basis, {'appraisals': [item]})
    store.view(now=now)
    store.pending_source_ids(before=now, limit=1)


def test_one_replys_reads_cost_the_same_statements_and_replay_one_record_at_any_history(tmp_path, monkeypatch):
    statements, replayed = [], []
    connect, original = sqlite3.connect, module._Ledger._replay

    def counting(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db
    monkeypatch.setattr(sqlite3, 'connect', counting)
    monkeypatch.setattr(module._Ledger, '_replay', lambda self, record, moment: replayed.append(1) or original(self, record, moment))
    measured = {}
    for count in (30, 3000):
        path = tmp_path / f'life-{count}.sqlite3'
        DailyLifeStore(path)
        now = seed(path, count) + timedelta(minutes=30)
        store = CharacterEmotionStore(path)
        store.view(now=now)  # the ledger is built once per process
        statements.clear(), replayed.clear()
        started = time.perf_counter()
        one_reply(store, now)
        measured[count] = (len(statements), len(replayed), time.perf_counter() - started)
    assert measured[30][:2] == measured[3000][:2]  # a fixed script of reads; the appended record alone is replayed
    assert measured[3000][1] == 1 and measured[3000][0] <= 60
    assert measured[3000][2] < 0.5  # about 0.045 s here; the whole-list replay took ~150 s at this size


def test_ledger_matches_the_reference_on_a_seeded_history(tmp_path):
    path = tmp_path / 'daily_life.sqlite3'
    DailyLifeStore(path)
    last = seed(path, 300)
    CharacterEmotionStore(path)
    with sqlite3.connect(path) as db:
        for minutes in (0, 700, 1400, 2099):
            assert_agrees(db, stamp(minutes), (stamp(minutes), 150) if minutes == 1400 else None, stamp(minutes))
        assert_agrees(db, module._time(last))
        assert _ledger_for(db).refused is None
