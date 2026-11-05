"""Deleting a letter rebuilds relationship state the way the live projection would have.

Stored histories are never in write order and repeat the same wording, so every
rebuild is compared with a reference ledger that was written without the letter.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import random
import sqlite3
import types

import pytest

from runtime.imports.letter_delete import Sources, apply_sql, world_plan
from runtime.memory.private_world_delivery import DeliveryEvent, PrivateWorldDeliveryCommitter
from runtime.memory.private_world_relationship import PrivateWorldRelationshipCommitter
from runtime.private_world.commands import GrantIntimacy, PrivateWorldActor, PrivateWorldCommandSource
from runtime.private_world.ledger import LedgerEvent, SQLitePrivateWorldLedger
from runtime.private_world.service import PrivateWorldCommandService
from runtime.relationship_growth import CHOICES

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
REPLAY = 'LETTER_DELETE_RELATIONSHIP_REPLAY_UNSUPPORTED'
LEGACY = 'LETTER_DELETE_LEGACY_RELATIONSHIP_UNSUPPORTED'
COMPARED = ('applied', 'reason_code', 'contact_qualification', 'initiative_tier', 'initiative_caution')


def letter(identity, hours, kind='support_received', quote='good night', **extra):
    return {'op': 'letter', 'id': identity, 'at': NOW + hours * HOUR, 'kind': kind, 'quote': quote, **extra}


def play(ledger, step):
    at = step['at']
    if step['op'] == 'letter':
        delivery = step['id'] + ':1'
        PrivateWorldDeliveryCommitter(ledger).commit(DeliveryEvent(delivery_id=delivery, occurred_at=at,
            semantic_key='canonical.' + step['id'], canonical_reply_sha256=hashlib.sha256(b'reply').hexdigest()))
        if step.get('exchange', True):
            signal = {'kind': step['kind'], 'user_quote': step['quote'], 'reply_quote': 'reply'}
            if step.get('signals'):
                signal.update(signals=step['signals'], moment=step['moment'])
            if step.get('stage'):
                signal['relationship_stage'] = step['stage']
            PrivateWorldRelationshipCommitter(ledger).commit_exchange(delivery, step['quote'], 'reply', signal, occurred_at=at)
            if step.get('day', True):
                PrivateWorldRelationshipCommitter(ledger).commit_interaction_day(delivery, 'reply', occurred_at=at)
    elif step['op'] == 'checkpoint':
        # What a writer without replayable inputs leaves behind: a new absolute state.
        state = ledger.snapshot()
        ledger.apply_once(LedgerEvent(step['id'], step['id'], 'opaque_legacy', {}, at.isoformat()),
            replace(state, version=state.version + 1, **step['changes']), expected_snapshot_version=state.version)
    elif step['op'] == 'route':
        from runtime import relationship_lite
        previous = relationship_lite.uuid
        relationship_lite.uuid = types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex=step['id']))
        try:
            relationship_lite.change_route(ledger, step['route'], ledger.snapshot().version, now=at)
        finally:
            relationship_lite.uuid = previous
    elif step['op'] == 'history_days':
        # The one-time repair that counts the days of a history imported by an earlier version.
        from runtime.imports.relationship_batches import add_history_days
        add_history_days(ledger, step['days'], at=at)
    elif step['op'] == 'baseline':
        from runtime.private_world.commands import InitializeHistoricalRelationship
        from runtime.reply.reply_context import RelationshipStage
        PrivateWorldCommandService(ledger).execute(InitializeHistoricalRelationship(
            command_id=step['id'], idempotency_key=step['id'], actor=PrivateWorldActor.MIGRATION,
            source=PrivateWorldCommandSource.IMPORT, occurred_at=at, reason='synthetic', evidence_refs=step['refs'],
            relationship_stage=RelationshipStage.UNKNOWN, familiarity=20, trust=20, comfort=20, closeness=0, tension=0))
    elif step['op'] == 'grant':
        from runtime.reply.reply_context import IntimacyTier
        PrivateWorldCommandService(ledger).execute(GrantIntimacy(command_id=step['id'], idempotency_key=step['id'],
            actor=PrivateWorldActor.LOCAL_USER, source=PrivateWorldCommandSource.CONTROL_CENTER, occurred_at=at,
            reason='synthetic', evidence_refs=(), grant_id='grant.' + step['id'], tier=IntimacyTier.LIGHT_CONTACT,
            statement='synthetic'))
    else:
        raise KeyError(step['op'])


def build(path, steps, skip=(), without_exchange=()):
    ledger = SQLitePrivateWorldLedger(path)
    for step in steps:
        if step['id'] in skip or step.get('with') in skip:
            continue
        play(ledger, {**step, 'exchange': False} if step['id'] in without_exchange else step)
    return ledger


def dump(ledger):
    with sqlite3.connect(ledger._database_path) as db:
        db.row_factory = sqlite3.Row
        return tuple({row[0]: dict(row) for row in db.execute(query)} for query in (
            'SELECT event_id,rowid AS _rowid_,* FROM private_world_events',
            'SELECT event_id,* FROM private_world_snapshots',
            'SELECT event_id,* FROM private_world_projection_inputs',
            'SELECT key,* FROM private_world_metadata'))


def content(ledger, row):
    state = ledger._strict_stored_snapshot(row['version'], row['payload_json']).to_dict()
    state.pop('version')
    return state


def assert_matches_reference(rebuilt, reference):
    """Every kept record, snapshot and derived field equals the ledger written without the letter."""
    events, snapshots, inputs, _ = dump(rebuilt)
    ref_events, ref_snapshots, ref_inputs, _ = dump(reference)
    kept = {event_id for event_id, event in events.items() if event['event_type'] != 'letter_source_removed'}
    assert kept == set(ref_events)
    ref_position = {event_id: event['_rowid_'] for event_id, event in ref_events.items()}
    ref_states = sorted((ref_position[event_id], content(reference, row)) for event_id, row in ref_snapshots.items())
    final = content(reference, max(ref_snapshots.values(), key=lambda row: row['version']))
    newest = max(row['version'] for row in snapshots.values())
    assert content(rebuilt, max(snapshots.values(), key=lambda row: row['version'])) == final
    for event_id, row in snapshots.items():
        if event_id in kept:
            # The newest snapshot also carries the effect of later events that store none of their own.
            expected = final if row['version'] == newest else [
                state for position, state in ref_states if position <= ref_position[event_id]][-1]
            assert content(rebuilt, row) == expected, events[event_id]['event_type']
    for event_id, row in ref_inputs.items():
        mine, theirs = json.loads(inputs[event_id]['result_json']), json.loads(row['result_json'])
        assert {key: mine.get(key) for key in COMPARED} == {key: theirs.get(key) for key in COMPARED}
    assert set(inputs) == set(ref_inputs)
    assert rebuilt.snapshot() == replace(reference.snapshot(), version=rebuilt.snapshot().version)


def delete(ledger, identity, part='reply'):
    patches = world_plan(ledger, Sources({'letter_id': identity}, part))
    apply_sql(ledger._database_path, patches)
    return patches


def refused_by_the_old_guard(ledger):
    """The conditions that used to stop every deletion (event time order and unique keys)."""
    events, _, inputs, _ = dump(ledger)
    replayed = sorted((event for event_id, event in events.items() if event_id in inputs), key=lambda e: e['_rowid_'])
    times = [datetime.fromisoformat(event['occurred_at']) for event in replayed]
    keys = [json.loads(event['payload_json']).get('semantic_key') for event in replayed]
    keys = [key for key in keys if key is not None]
    return times != sorted(times) or len(keys) != len(set(keys))


def check(tmp_path, steps, victim, part='reply'):
    work = tmp_path / f'{part}-{victim}'
    work.mkdir()
    original = build(work / 'original.db', steps)
    reference = build(work / 'reference.db', steps, skip={victim} if part == 'reply' else (),
                      without_exchange={victim} if part == 'user' else ())
    before = dump(original)
    patches = delete(original, victim, part)
    assert_matches_reference(original, reference)
    with sqlite3.connect(original._database_path) as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    apply_sql(original._database_path, patches)  # an interrupted deletion is retried safely
    assert_matches_reference(original, reference)
    apply_sql(original._database_path, patches, restore=True)
    assert dump(original) == before
    return before, patches


HISTORY = [
    letter('a', 0), letter('b', 2), letter('c', -5), letter('d', 48, 'meaningful_exchange', 'tired', stage='familiar'),
]


@pytest.mark.parametrize('victim', ['a', 'b', 'c', 'd'])
def test_interleaved_times_and_repeated_wording_before_the_deleted_letter(tmp_path, victim):
    original = build(tmp_path / 'check.db', HISTORY)
    # Exactly what used to refuse every deletion: event time out of write order and one wording used on three letters.
    assert refused_by_the_old_guard(original)
    check(tmp_path, HISTORY, victim)


def test_deleting_the_earliest_wording_counts_the_surviving_copy_again(tmp_path):
    steps = [letter('first', 0, quote='miss you'), letter('second', 3, quote='miss you'), letter('other', 30, quote='x')]
    check(tmp_path, steps, 'first')
    original = build(tmp_path / 'again.db', steps)
    second = 'relationship.' + hashlib.sha256(('exchange.' + hashlib.sha256(b'second:1').hexdigest()).encode()).hexdigest()
    outcome = lambda: json.loads(dump(original)[2][second]['result_json'])
    assert outcome()['reason_code'] == 'SEMANTIC_DUPLICATE'
    trust = original.snapshot().trust
    delete(original, 'first')
    assert outcome()['applied'] is True and outcome()['reason_code'] == 'SUPPORT_RECEIVED'
    assert original.snapshot().trust == trust  # one copy still counts once


@pytest.mark.parametrize('victim', ['early', 'late', 'middle'])
def test_late_letters_written_after_the_deleted_one_are_replayed_in_event_time_order(tmp_path, victim):
    steps = [
        letter('early', 10, 'shared_experience', 'walk'), letter('late', 40, 'conflict', 'quarrel'),
        letter('middle', 20, 'support_received', 'walk'),
        # Written after all of them, but with the oldest time and the same wording.
        letter('written-last', 1, 'support_received', 'walk', stage='close'),
        letter('written-very-last', 45, 'repair', 'sorry'),
    ]
    assert refused_by_the_old_guard(build(tmp_path / 'check.db', steps))
    check(tmp_path, steps, victim)


def test_letters_sharing_one_event_time_replay_in_projection_order(tmp_path):
    steps = [letter(name, 5, kind, quote=quote, stage=stage) for name, kind, quote, stage in [
        ('x', 'conflict', 'a', None), ('y', 'support_received', 'b', 'close'),
        ('z', 'boundary_respected', 'c', None), ('w', 'repair', 'd', None)]]
    for victim in ('x', 'y', 'z', 'w'):
        check(tmp_path, steps, victim)


def test_relationship_evidence_with_weekly_allowances_is_rebuilt_exactly(tmp_path):
    steps = [letter('L%d' % i, hours, 'relationship_evidence', 'same', signals=signals, moment=moment)
             for i, (hours, signals, moment) in enumerate([
                 (30, {'rel_disclosure': 'vulnerable'}, 'rel_disclosure:vulnerable'),
                 (2, {'rel_care': 'mutual', 'rel_affection': 'warm'}, 'rel_care:mutual'),
                 (50, {'rel_disclosure': 'personal'}, 'rel_disclosure:personal'),
                 (26, {'rel_friction': 'conflict'}, 'rel_friction:conflict'),
                 (27, {'rel_friction': 'repair'}, 'rel_friction:repair')])]
    for victim in ('L0', 'L1', 'L3'):
        check(tmp_path, steps, victim)


def test_user_side_deletion_removes_only_the_relationship_evidence(tmp_path):
    check(tmp_path, HISTORY, 'c', part='user')


def test_deleting_both_sides_one_after_the_other(tmp_path):
    original = build(tmp_path / 'original.db', HISTORY)
    reference = build(tmp_path / 'reference.db', HISTORY, skip={'b'})
    delete(original, 'b', 'user')
    delete(original, 'b', 'reply')
    assert_matches_reference(original, reference)


def test_sequential_deletions_then_new_letters_match_a_ledger_that_never_had_them(tmp_path):
    later = [letter('n1', 100, quote='good night'), letter('n2', 130, 'conflict', 'again')]
    original = build(tmp_path / 'original.db', HISTORY)
    reference = build(tmp_path / 'reference.db', HISTORY, skip={'a', 'd'})
    delete(original, 'a')
    delete(original, 'd')
    assert_matches_reference(original, reference)
    for step in later:
        play(original, step)
        play(reference, step)
    assert original.snapshot() == replace(reference.snapshot(), version=original.snapshot().version)


def test_restore_after_new_letters_does_not_conflict(tmp_path):
    original = build(tmp_path / 'original.db', HISTORY)
    before = dump(original)
    patches = delete(original, 'c')
    play(original, letter('new', 100, 'conflict', 'new'))
    apply_sql(original._database_path, patches, restore=True)
    for table_before, table_now in zip(before, dump(original)):
        assert all(table_now[key] == row for key, row in table_before.items())


def test_checkpoint_before_the_deleted_letter_keeps_its_unrelated_state(tmp_path):
    steps = [letter('a', 0), letter('b', 3, quote='other'),
             {'op': 'checkpoint', 'id': 'old', 'at': NOW + 5 * HOUR,
              'changes': {'nickname_permissions': ('kept',), 'trust': 20}},
             letter('c', 30, 'conflict', 'x'), letter('d', 6), letter('e', 40)]
    for victim in ('c', 'd', 'e'):
        check(tmp_path, steps, victim)
    original = build(tmp_path / 'kept.db', steps)
    delete(original, 'c')
    assert original.snapshot().nickname_permissions == ('kept',)


def test_relationship_choice_after_the_deleted_letter_is_made_again_on_the_rebuilt_state(tmp_path):
    steps = [letter('a', 0, stage='close'), letter('b', 3, quote='other'), letter('c', 30, 'shared_experience', 'x'),
             {'op': 'route', 'id': 'chosen', 'at': NOW + 40 * HOUR, 'route': 'romantic'},
             letter('d', 50), letter('e', 20)]
    for victim in ('a', 'b', 'c', 'd', 'e'):
        check(tmp_path, steps, victim)


def test_counted_history_days_after_the_deleted_letter_are_counted_again_on_the_rebuilt_state(tmp_path):
    days = tuple(f'2026-09-{day:02}' for day in range(1, 12))
    steps = [letter('a', 0, 'meaningful_exchange', 'tired', stage='familiar'), letter('b', 3, quote='other'),
             letter('c', 30, 'shared_experience', 'x'),
             {'op': 'history_days', 'id': 'days', 'at': NOW + 40 * HOUR, 'days': days},
             letter('d', 50), letter('e', 20, quote='late')]
    for victim in ('a', 'b', 'c', 'd', 'e'):
        check(tmp_path, steps, victim)
    original = build(tmp_path / 'kept.db', steps)
    delete(original, 'a')
    assert set(days) <= set(original.snapshot().relationship_days)


@pytest.mark.parametrize('refs, victims', [(('source.import',), ('a', 'b', 'c')), (('a:1',), ('a',))])
def test_import_baseline_survives_an_earlier_late_letter_and_its_own_deletion(tmp_path, refs, victims):
    steps = [{'op': 'baseline', 'id': 'baseline', 'at': NOW, 'refs': refs, 'with': 'a' if refs == ('a:1',) else None},
             letter('a', 5), letter('b', -4), letter('c', 30)]
    for victim in victims:
        check(tmp_path, steps, victim)


def test_a_command_refused_when_given_stays_refused_after_a_deletion(tmp_path):
    steps = [{'op': 'grant', 'id': 'grant', 'at': NOW + 10 * HOUR}, letter('s', 0, stage='close', quote='s'),
             letter('a', 3, quote='a'), letter('b', 12, quote='a')]
    for victim in ('s', 'a', 'b'):
        check(tmp_path, steps, victim)


def test_late_stage_confirmation_behind_a_checkpoint_still_cannot_move_the_stage_back(tmp_path):
    steps = [letter('a', 10, stage='close', quote='a'),
             {'op': 'route', 'id': 'chosen', 'at': NOW + 20 * HOUR, 'route': 'romantic'},
             letter('v', 21, quote='v'), letter('s', 5, quote='s', stage='committed'), letter('w', 30, quote='w')]
    for victim in ('a', 'v', 's', 'w'):
        check(tmp_path, steps, victim)


def histories(seed):
    kinds = ['support_received', 'meaningful_exchange', 'shared_experience', 'conflict', 'repair', 'boundary_respected']
    rng = random.Random(seed)
    steps = []
    for index in range(rng.randrange(4, 10)):
        extra = {}
        if rng.random() < 0.25:
            extra['stage'] = rng.choice(['acquaintance', 'familiar', 'close'])
        if rng.random() < 0.4:
            keys = rng.sample(sorted(CHOICES), rng.randrange(1, 4))
            signals = {key: rng.choice(CHOICES[key][1:]) for key in keys}
            extra.update(signals=signals, moment=keys[0] + ':' + signals[keys[0]])
        steps.append(letter('L%d' % index, rng.choice([0, 0, 1, 9, 25, 50, 70]) + rng.randrange(0, 3) / 3,
                            'relationship_evidence' if extra.get('signals') else rng.choice(kinds),
                            rng.choice(['good night', 'miss you', 'tired', 'thanks']), day=rng.random() < 0.7, **extra))
    rng.shuffle(steps)
    victim = rng.choice(steps)
    # A stage confirmation written only because of this letter has nothing to compare with.
    victim.pop('stage', None)
    return steps, victim['id']


@pytest.mark.parametrize('seed', range(24))
def test_random_histories_match_a_ledger_written_without_the_letter(tmp_path, seed):
    steps, victim = histories(seed)
    original = build(tmp_path / 'original.db', steps)
    reference = build(tmp_path / 'reference.db', steps, skip={victim})
    delete(original, victim)
    assert_matches_reference(original, reference)


def test_unexplained_checkpoint_after_the_deleted_letter_is_refused_and_nothing_changes(tmp_path):
    steps = [letter('a', 0), letter('b', 3, quote='other'),
             {'op': 'checkpoint', 'id': 'old', 'at': NOW + 5 * HOUR, 'changes': {'nickname_permissions': ('kept',)}},
             letter('c', 30, 'conflict', 'x')]
    ledger = build(tmp_path / 'ledger.db', steps)
    before = dump(ledger)
    with pytest.raises(ValueError, match=LEGACY):
        world_plan(ledger, Sources({'letter_id': 'a'}, 'reply'))
    assert dump(ledger) == before
    # A letter the checkpoint did not build on can still be removed.
    assert world_plan(ledger, Sources({'letter_id': 'c'}, 'reply'))


def test_history_that_the_replay_cannot_reproduce_is_refused(tmp_path):
    ledger = build(tmp_path / 'ledger.db', HISTORY)
    with sqlite3.connect(ledger._database_path) as db:
        newest = db.execute('SELECT version FROM private_world_snapshots ORDER BY version DESC').fetchone()[0]
        tampered = replace(ledger.snapshot(), trust=ledger.snapshot().trust + 7)
        db.execute('UPDATE private_world_snapshots SET payload_json=? WHERE version=?', (ledger._snapshot_json(tampered), newest))
    before = dump(ledger)
    with pytest.raises(ValueError, match=REPLAY):
        world_plan(ledger, Sources({'letter_id': 'a'}, 'reply'))
    assert dump(ledger) == before


def test_corrupt_stored_input_is_refused_not_guessed(tmp_path):
    ledger = build(tmp_path / 'ledger.db', HISTORY)
    with sqlite3.connect(ledger._database_path) as db:
        db.execute("UPDATE private_world_projection_inputs SET payload_json='{}'")
    with pytest.raises(ValueError, match=REPLAY):
        world_plan(ledger, Sources({'letter_id': 'a'}, 'reply'))


def test_a_letter_without_relationship_records_changes_no_relationship_state(tmp_path):
    ledger = build(tmp_path / 'ledger.db', [letter('only-delivery', 0, exchange=False)] + HISTORY)
    before = dump(ledger)
    patches = world_plan(ledger, Sources({'letter_id': 'only-delivery'}, 'reply'))
    assert {patch['table'] for patch in patches} == {'private_world_events'}
    apply_sql(ledger._database_path, patches)
    assert dump(ledger)[1:] == before[1:]


def test_removing_a_letter_that_changed_nothing_leaves_a_later_checkpoint_alone(tmp_path):
    steps = [letter('a', 0), letter('copy', 2),  # the same wording within a day changes nothing
             {'op': 'checkpoint', 'id': 'old', 'at': NOW + 5 * HOUR, 'changes': {'nickname_permissions': ('kept',)}},
             letter('c', 30, 'conflict', 'x')]
    check(tmp_path, steps, 'copy')
    with pytest.raises(ValueError, match=LEGACY):
        check(tmp_path, steps, 'a')


def test_a_reached_volume_is_never_lowered_by_a_rebuilt_state():
    from runtime.imports.letter_delete_replay import _floored
    from runtime.private_world.port import PrivateWorldSnapshot
    current = PrivateWorldSnapshot(relationship_volume=4, familiarity=50, trust=50, comfort=50,
                                   relationship_days=('2026-10-01', '2026-10-02'), volume_started_days=2)
    rebuilt = PrivateWorldSnapshot(relationship_volume=2, familiarity=10, trust=60, comfort=5, relationship_days=('2026-10-01',))
    result = _floored(rebuilt, current)
    assert (result.relationship_volume, result.familiarity, result.trust, result.comfort, result.volume_started_days) == (4, 35, 60, 35, 1)
    assert _floored(current, rebuilt) is current


def test_a_history_too_tangled_to_replay_in_time_is_refused(tmp_path, monkeypatch):
    from runtime.imports import letter_delete_replay
    ledger = build(tmp_path / 'ledger.db', HISTORY)
    monkeypatch.setattr(letter_delete_replay, '_STEP_LIMIT', 3)
    with pytest.raises(ValueError, match=REPLAY):
        world_plan(ledger, Sources({'letter_id': 'a'}, 'reply'))
