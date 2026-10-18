"""Deleting letters end to end: the real local backend, synthetic letters, the calls the window makes.

Every test starts a real backend process (``letter_delete_e2e_backend.py``: the product's ``local_server`` and its
original-client application on a free loopback port, a fresh data directory, the real Mem0 store with a stand-in
embedder). The letters are sent through ``/toy/letter/send``; the memories, the original-text index, daily life
records, relationship events and the semantic vectors are written by the product's own background writers. Then the
tests call the same deletion, restore and memory-browser routes as the letters-and-memories window, and read every
store back to check what changed after the deletion and what came back after the restore.

Needs the Mem0 and Qdrant packages: installed in this interpreter, or a folder named by ``OLIVIA_E2E_MEM0_SITE``.
Set ``OLIVIA_E2E_EVIDENCE`` to a file path to write the before / after-delete / after-restore counts as JSON.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.imports.letter_delete_e2e_support import (
    BY_KEY, OLD_LETTERS, SYNTHETIC, Backend, comparable, counts, differences, import_old_letters, mem0_site,
    relationship_levels, script_for, seed, wait_for_history_batches, where,
)

SITE = mem0_site()
pytestmark = pytest.mark.skipif(SITE is None, reason='Mem0 and Qdrant are not installed (see OLIVIA_E2E_MEM0_SITE)')


@pytest.fixture(scope='module')
def evidence():
    record = {}
    yield record
    target = os.environ.get('OLIVIA_E2E_EVIDENCE')
    if target:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')


@pytest.fixture
def world(tmp_path):
    """A backend that has received, answered and remembered the five synthetic letters."""
    backend = Backend(tmp_path / 'app', script_for(), SITE)
    try:
        backend.wait_ready()
        ids = seed(backend.api)
        yield SimpleNamespace(backend=backend, api=backend.api, ids=ids, before=backend.api.snapshot())
        assert backend.blocked_connections == [], 'the backend tried to leave this computer'
    finally:
        backend.stop()


@pytest.fixture(scope='module')
def reference(tmp_path_factory):
    """The relationship numbers of a backend that never received some of the letters."""
    cache, started = {}, []

    def levels(*without):
        key = tuple(sorted(without))
        if key not in cache:
            backend = Backend(tmp_path_factory.mktemp('reference'), script_for(), SITE)
            started.append(backend)
            backend.wait_ready()
            seed(backend.api, [e for e in SYNTHETIC if e['key'] not in key])
            cache[key] = relationship_levels(backend.api.snapshot())
            backend.stop()
        return cache[key]

    yield levels
    for backend in started:
        backend.stop()


def restore_all(api, *, newest_first=False):
    """Press 恢复 on the first row of 最近删除 until the list is empty; the window reloads the list after each one."""
    for _ in range(10):
        items = [item for item in api.trash() if item['status'] == 'deleted']
        if not items:
            return
        pick = max(items, key=lambda t: t['deleted_at']) if newest_first else items[0]
        answer = api.restore(pick['id'])
        assert answer['data']['status'] == 'RESTORED', answer
    raise AssertionError('the recently deleted list does not empty')


def settled(world):
    world.api.settle()
    return world.api.snapshot()


def api_view(api):
    """Everything the window can read: the letters, the memory list and the original-text search."""
    letters = {row['letter_id']: api.detail(row['letter_id']) for row in api.letters()}
    return {
        'letters': {lid: (d['content'], d['reply_text']) for lid, d in letters.items()},
        'memories': sorted((m['memory_id'], m['text'], m['source_id']) for m in api.memories()),
        'originals': sorted((o['source_id'], o['speaker'], o['text']) for o in api.originals()),
    }


def memory_of(snapshot, exchange):
    return [p for p in snapshot['points'] if exchange['user'] in p['payload'].get('data', '')]


def assert_side_gone(snapshot, exchange, side):
    """No store holds the text of this side of the exchange, or any quote taken from it."""
    for text in (exchange[side], *exchange['markers'][side]):
        assert where(snapshot, text) == [], f'{side} text of {exchange["key"]} is still stored: {text}'


def assert_side_kept(snapshot, exchange, side):
    assert 'originals.originals' in where(snapshot, exchange[side]), f'{side} text of {exchange["key"]} was lost'


def garden(snapshot):
    rows = [json.loads(r['payload']) for r in snapshot['databases']['life']['life_projects'] if r['id'] == 'garden']
    return rows[0] if rows else None


# --------------------------------------------------------------------------------------------------------------
def test_delete_my_letter_only_keeps_her_reply(world, reference, evidence):
    api, before = world.api, world.before
    exchange, letter = BY_KEY['garden-done'], world.ids['garden-done']
    memories_before = {m['memory_id']: m for m in api.memories()}
    assert len(memories_before) == 5 and len(memory_of(before, exchange)) == 1
    assert api.detail(letter)['content'] == exchange['user']
    assert [o['speaker'] for o in api.originals(query='给它浇了水')] == ['user']

    result = api.delete(letter, 'user')
    assert (result['http'], result['code'], result['data']['status']) == (200, 0, 'DELETED')
    after = settled(world)

    # the mailbox: my letter is empty, her reply and the letter itself stay
    detail = api.detail(letter)
    assert (detail['content'], detail['reply_text']) == ('', exchange['reply'])
    assert letter in {row['letter_id'] for row in api.letters()}
    # the original text: mine is gone from the search, hers is still found
    assert api.originals(query='给它浇了水') == []
    assert [o['speaker'] for o in api.originals(query='多晒太阳')] == ['linli']
    # memories: exactly the memory made from my letter is gone, the other four are untouched
    gone = {m['id'] for m in memory_of(before, exchange)}
    now = {m['memory_id']: m for m in api.memories()}
    assert set(memories_before) - set(now) == gone and len(now) == 4
    assert all(now[key] == memories_before[key] for key in now)
    # nothing else keeps my words, in any store, any quote or any derived record
    assert_side_gone(after, exchange, 'user')
    assert_side_kept(after, exchange, 'reply')
    # the garden project is shared with an earlier letter: it goes back to that letter's state instead of disappearing
    project = garden(after)
    assert project['status'] == 'planned' and project['source_id'] == f'reply:{world.ids["garden-plan"]}:1'
    # the relationship is what it would be had this letter never been written
    assert relationship_levels(after) == reference('garden-done')
    assert counts(after)['relationship']['moments'] == 4 and counts(after)['relationship']['removed_markers'] == 1
    assert [(t['letter_id'], t['part']) for t in api.trash()] == [(letter, 'user')]

    # restore: every store is back exactly as it was
    trash_id = api.trash()[0]['id']
    restored = api.restore(trash_id)
    assert (restored['code'], restored['data']['status']) == (0, 'RESTORED')
    final = settled(world)
    assert comparable(final) == comparable(before), differences(comparable(before), comparable(final))
    assert api.detail(letter)['content'] == exchange['user'] and api.trash() == []
    assert len(api.memories()) == 5 and garden(final)['status'] == 'ongoing'
    evidence['delete_my_letter_only'] = {'before': counts(before), 'after_delete': counts(after), 'after_restore': counts(final)}


def test_delete_her_reply_only_keeps_my_letter_and_my_memory(world, reference, evidence):
    api, before = world.api, world.before
    exchange, letter = BY_KEY['garden-done'], world.ids['garden-done']
    memories_before = {m['memory_id']: m for m in api.memories()}

    assert api.delete(letter, 'reply')['data']['status'] == 'DELETED'
    after = settled(world)

    detail = api.detail(letter)
    assert (detail['content'], detail['reply_text']) == (exchange['user'], '')
    assert api.originals(query='多晒太阳') == []
    assert [o['speaker'] for o in api.originals(query='给它浇了水')] == ['user']
    # her reply made no memory: all five memories stay, in particular the one made from my own words
    assert {m['memory_id']: m for m in api.memories()} == memories_before
    assert_side_gone(after, exchange, 'reply')
    assert_side_kept(after, exchange, 'user')
    # the relationship evidence needs both sides of the exchange, so it is withdrawn and the numbers are rebuilt
    assert relationship_levels(after) == reference('garden-done')
    assert counts(after)['relationship']['moments'] == 4

    api_restore = api.restore(api.trash()[0]['id'])
    assert api_restore['data']['status'] == 'RESTORED'
    final = settled(world)
    assert comparable(final) == comparable(before), differences(comparable(before), comparable(final))
    assert api.detail(letter)['reply_text'] == exchange['reply']
    evidence['delete_her_reply_only'] = {'before': counts(before), 'after_delete': counts(after), 'after_restore': counts(final)}


def test_delete_both_sides_of_one_exchange(world, reference, evidence):
    api, before = world.api, world.before
    exchange, letter = BY_KEY['garden-done'], world.ids['garden-done']

    assert api.delete(letter, 'user')['data']['status'] == 'DELETED'
    assert api.delete(letter, 'reply')['data']['status'] == 'DELETED'
    after = settled(world)

    assert letter not in {row['letter_id'] for row in api.letters()} and len(api.letters()) == 4
    assert api.originals(query='种好的花') == [] and len(api.memories()) == 4
    assert_side_gone(after, exchange, 'user')
    assert_side_gone(after, exchange, 'reply')
    assert relationship_levels(after) == reference('garden-done')
    # my text was indexed twice (as received and as part of the exchange), hers once; the exchange's vector and
    # relationship quote go with it
    was = counts(before)['originals']
    assert counts(after)['originals'] == {'user': was['user'] - 2, 'linli': was['linli'] - 1,
                                          'search_chunks': was['search_chunks'] - 3, 'vectors': was['vectors'] - 2,
                                          'relationship_quotes': was['relationship_quotes'] - 1}
    assert garden(after)['status'] == 'planned'
    assert sorted(t['part'] for t in api.trash()) == ['reply', 'user']

    # one restore brings back the whole exchange: both items leave the recently deleted list
    assert api.restore(api.trash()[0]['id'])['data']['status'] == 'RESTORED'
    final = settled(world)
    assert api.trash() == []
    assert comparable(final) == comparable(before), differences(comparable(before), comparable(final))
    evidence['delete_both_sides'] = {'before': counts(before), 'after_delete': counts(after), 'after_restore': counts(final)}


def test_multi_select_deletes_two_exchanges_in_one_action(world, reference, evidence):
    api, before = world.api, world.before
    plan, piano = BY_KEY['garden-plan'], BY_KEY['piano']
    assert counts(before)['relationship']['candidates'] == 1

    # the window sends one call per ticked side, in the order of the list
    selection = [(world.ids['garden-plan'], 'user'), (world.ids['garden-plan'], 'reply'), (world.ids['piano'], 'user')]
    assert [api.delete(*item)['data']['status'] for item in selection] == ['DELETED'] * 3
    after = settled(world)

    assert_side_gone(after, plan, 'user')
    assert_side_gone(after, plan, 'reply')
    assert_side_gone(after, piano, 'user')
    assert_side_kept(after, piano, 'reply')
    assert len(api.memories()) == 3
    assert counts(after)['relationship']['candidates'] == 0, 'the candidate made from the deleted piano letter is gone'
    assert relationship_levels(after) == reference('garden-plan', 'piano')
    project = garden(after)
    assert project['status'] == 'ongoing' and project['source_id'] == f'reply:{world.ids["garden-done"]}:1'
    assert len(api.trash()) == 3

    # the oldest row is restored first: the plan exchange comes back with both of its sides, the piano letter, which
    # was deleted after it and shares the relationship numbers with it, stays deleted
    piano_deleted_at = next(t['deleted_at'] for t in api.trash() if t['letter_id'] == world.ids['piano'])
    assert api.restore(min(api.trash(), key=lambda t: t['deleted_at'])['id'])['data']['status'] == 'RESTORED'
    middle = settled(world)
    assert_side_kept(middle, plan, 'user')
    assert_side_kept(middle, plan, 'reply')
    assert_side_gone(middle, piano, 'user')
    assert relationship_levels(middle) == reference('piano')
    assert [(t['letter_id'], t['part'], t['deleted_at']) for t in api.trash()] == [(world.ids['piano'], 'user', piano_deleted_at)]
    assert api.detail(world.ids['garden-plan'])['content'] == plan['user'] and len(api.memories()) == 4

    # restore the rest
    restore_all(api)
    final = settled(world)
    assert api.trash() == []
    assert comparable(final) == comparable(before), differences(comparable(before), comparable(final))
    evidence['multi_select_two_exchanges'] = {'before': counts(before), 'after_delete': counts(after), 'after_restore': counts(final)}


def test_restore_brings_back_letters_originals_memories_and_relationship_exactly(world):
    api, before = world.api, world.before
    view_before = api_view(api)
    moves = [('ice', 'user'), ('rain', 'reply'), ('piano', 'user'), ('piano', 'reply'), ('garden-done', 'user')]
    for key, part in moves:
        assert api.delete(world.ids[key], part)['data']['status'] == 'DELETED'
    changed = settled(world)
    assert comparable(changed) != comparable(before)
    assert api_view(api) != view_before

    restore_all(api, newest_first=True)
    final = settled(world)
    assert api.trash() == []
    assert comparable(final) == comparable(before), differences(comparable(before), comparable(final))
    assert api_view(api) == view_before
    assert relationship_levels(final) == relationship_levels(before)


def test_a_deleted_source_cannot_come_back_through_indexers_or_a_restart(world):
    api = world.api
    for key, part in (('garden-done', 'user'), ('garden-done', 'reply'), ('piano', 'user')):
        assert api.delete(world.ids[key], part)['data']['status'] == 'DELETED'
    deleted = settled(world)
    rows = comparable(deleted)

    # every indexer the app runs in the background: the memory outbox, the received-text index, the vectors,
    # the relationship batch
    assert api.reindex()['outbox'] is True
    api.relationship()
    again = settled(world)
    assert comparable(again) == rows, differences(rows, comparable(again))

    # closing and opening the app runs the start-up recovery of every writer
    world.backend.restart()
    api = world.api = world.backend.api
    api.reindex()
    api.relationship()
    restarted = settled(world)
    assert comparable(restarted) == rows, differences(rows, comparable(restarted))
    for key, side in (('garden-done', 'user'), ('garden-done', 'reply'), ('piano', 'user')):
        assert_side_gone(restarted, BY_KEY[key], side)
    assert len(api.memories()) == 3 and api.originals(query='十六小节') == []

    # and the restore still works after the restart
    restore_all(api)
    final = settled(world)
    assert comparable(final) == comparable(world.before), differences(comparable(world.before), comparable(final))


def test_memory_browser_linked_originals_and_letter_search_follow_the_deletion(world):
    api = world.api
    exchange, letter = BY_KEY['garden-done'], world.ids['garden-done']
    memory = next(m for m in api.memories() if exchange['user'] in m['text'])
    both = api.originals(source_id=memory['source_id'])
    assert sorted(o['speaker'] for o in both) == ['linli', 'user']

    # her reply is deleted, my memory stays: its linked original no longer shows her deleted text
    api.delete(letter, 'reply')
    settled(world)
    assert any(m['memory_id'] == memory['memory_id'] for m in api.memories())
    linked = api.originals(source_id=memory['source_id'])
    assert [o['speaker'] for o in linked] == ['user']
    assert all(exchange['reply'] not in o['text'] and '多晒太阳' not in o['text'] for o in linked)
    assert api.originals(query='多晒太阳') == []

    # my letter is deleted as well: the memory is gone, so is the letters-tab search result for my words
    api.delete(letter, 'user')
    settled(world)
    assert not any(m['memory_id'] == memory['memory_id'] for m in api.memories())
    assert api.originals(source_id=memory['source_id']) == []
    assert api.originals(query='白色的花已经种好了') == []
    # the other letters are searchable as before
    assert sorted(o['speaker'] for o in api.originals(query='听雨')) == ['linli', 'user']


def test_a_letter_deleted_before_the_relationship_batch_never_reaches_it(tmp_path):
    backend = Backend(tmp_path / 'app', script_for(), SITE)
    try:
        backend.wait_ready()
        api = backend.api
        ids = seed(api, relationship=False)
        plan, letter = BY_KEY['garden-plan'], ids['garden-plan']
        before = api.snapshot()
        queue = before['databases']['world']['relationship_queue']
        assert len(queue) == 5 and all(row['status'] == 'pending' for row in queue)

        assert api.delete(letter, 'user')['data']['status'] == 'DELETED'
        deleted = settled(SimpleNamespace(api=api))
        # the waiting turn is withdrawn with the letter: its text is not kept for the batch
        assert [row['message_id'] for row in deleted['databases']['world']['relationship_queue']
                if row['message_id'] == letter] == []
        assert_side_gone(deleted, plan, 'user')

        assert api.relationship()['result'] == 'updated'
        after = settled(SimpleNamespace(api=api))
        judged = {e['delivery_id'] for e in after['databases']['world']['private_world_events']
                  if e['event_type'] == 'relationship_evidence'}
        assert len(judged) == 4, 'the deleted letter was judged by the batch after all'
        assert_side_gone(after, plan, 'user')
        assert len(after['databases']['world']['relationship_moments']) == 4

        # restored, the turn waits again and the next batch judges it
        assert api.restore(api.trash()[0]['id'])['data']['status'] == 'RESTORED'
        restored = settled(SimpleNamespace(api=api))
        assert len(restored['databases']['world']['relationship_queue']) == 5
        assert api.relationship()['result'] == 'updated'
        final = settled(SimpleNamespace(api=api))
        assert len([e for e in final['databases']['world']['private_world_events']
                    if e['event_type'] == 'relationship_evidence']) == 5
        assert backend.blocked_connections == []
    finally:
        backend.stop()


def diary_of_that_day(snapshot):
    return [r for r in snapshot['databases']['diary'].get('diary_entries', []) if len(r['day']) == 10]


def quoting_days(snapshot, text):
    return [r['day'] for r in snapshot['databases']['diary'].get('diary_entries', []) if text in r['body']]


def test_her_diary_stays_as_written_when_the_letter_is_deleted(world, evidence):
    """The deletion always goes ahead. Her diary is her own writing and is not changed unless the user also asks."""
    api = world.api
    exchange, letter = BY_KEY['garden-done'], world.ids['garden-done']
    api.diary(add=True, text=exchange['user'])  # her diary of that day quotes the user's letter
    with_diary = settled(world)
    assert len(api.diary_offer([letter])['days']) == 1  # what the confirmation asks about

    assert api.delete(letter, 'user')['data']['status'] == 'DELETED'
    after = settled(world)
    assert api.detail(letter)['content'] == '' and len(api.memories()) == 4
    assert after['databases']['diary'] == with_diary['databases']['diary'], 'her diary was changed'
    assert counts(after)['diary'] == {'entries': 2, 'comments': 1, 'days_to_write_again': 0}
    # the diary is the one place that still quotes the letter, as it was written; the tick leaves it alone
    assert where(after, exchange['user']) == ['diary.diary_entries']
    assert exchange['user'] in api.diary_tick()[diary_of_that_day(after)[0]['day']]

    restore_all(api)
    final = settled(world)
    assert comparable(final) == comparable(with_diary), differences(comparable(with_diary), comparable(final))
    evidence['delete_my_letter_diary_kept'] = {'before': counts(with_diary), 'after_delete': counts(after), 'after_restore': counts(final)}


def test_her_diary_of_that_day_is_written_again_without_the_deleted_letter(world, evidence):
    api = world.api
    exchange, letter = BY_KEY['garden-done'], world.ids['garden-done']
    api.diary(add=True, text=exchange['user'])
    with_diary = settled(world)
    (original,) = diary_of_that_day(with_diary)

    assert api.delete(letter, 'user', with_diary=True)['data']['status'] == 'DELETED'
    after = settled(world)
    # the day's entry and its comment are gone and the day is queued; the memoir of the month is not touched
    assert counts(after)['diary'] == {'entries': 1, 'comments': 0, 'days_to_write_again': 1}
    assert diary_of_that_day(after) == []
    # the memoir of the month is the one record that still quotes the letter, as written
    month = [r['day'] for r in with_diary['databases']['diary']['diary_entries'] if len(r['day']) == 7]
    assert quoting_days(after, exchange['user']) == month and where(after, exchange['user']) == ['diary.diary_entries']

    # the diary writer runs: the day is written again from what is left of its letters
    entries = api.diary_tick()
    rewritten = entries[original['day']]
    assert rewritten.startswith('那天的信：') and exchange['user'] not in rewritten
    assert BY_KEY['ice']['user'] in rewritten and exchange['reply'] in rewritten  # her reply to the deleted letter stays
    written = settled(world)
    # the new entry does not quote the deleted letter; only the memoir of the month does, unchanged
    assert quoting_days(written, exchange['user']) == month and where(written, exchange['user']) == ['diary.diary_entries']
    assert counts(written)['diary'] == {'entries': 2, 'comments': 0, 'days_to_write_again': 0}
    assert api.diary_tick()[original['day']] == rewritten, 'written once, not again'

    # restore: the original entry and its comment replace the new one
    assert api.restore(api.trash()[0]['id'])['data']['status'] == 'RESTORED'
    final = settled(world)
    assert [(r['day'], r['body']) for r in diary_of_that_day(final)] == [(original['day'], original['body'])]
    assert api.diary_tick()[original['day']] == original['body'], 'nothing is written after the restore'
    final = settled(world)
    assert comparable(final) == comparable(with_diary), differences(comparable(with_diary), comparable(final))
    evidence['delete_my_letter_diary_written_again'] = {
        'before': counts(with_diary), 'after_delete': counts(after), 'after_written_again': counts(written), 'after_restore': counts(final)}


def test_a_day_older_than_the_caught_up_days_is_still_written_again_once(world):
    api = world.api
    exchange, letter = BY_KEY['garden-done'], world.ids['garden-done']
    api.diary(add=True, text=exchange['user'])
    day = diary_of_that_day(settled(world))[0]['day']
    assert api.delete(letter, 'user', with_diary=True)['data']['status'] == 'DELETED'
    settled(world)
    # ten days on, the day is far outside the days the diary catches up; the request still brings it back
    entries = api.diary_tick(days_ahead=10)
    assert entries[day].startswith('那天的信：') and exchange['user'] not in entries[day]
    again = settled(world)
    assert counts(again)['diary']['days_to_write_again'] == 0
    assert api.diary_tick(days_ahead=10)[day] == entries[day]


def test_the_diary_choice_follows_the_deletion_when_a_later_one_is_undone_and_done_again(world):
    """The first letter is deleted without the diary, a second one after it with it. Restoring the older one keeps the choice."""
    api = world.api
    api.diary(add=True, text=BY_KEY['garden-plan']['user'])
    with_diary = settled(world)
    plan, done = world.ids['garden-plan'], world.ids['garden-done']
    assert api.delete(plan, 'user')['data']['status'] == 'DELETED'  # the diary stays
    assert api.delete(done, 'user', with_diary=True)['data']['status'] == 'DELETED'  # this one has the day written again
    assert counts(settled(world))['diary'] == {'entries': 1, 'comments': 0, 'days_to_write_again': 1}

    older = min(api.trash(), key=lambda item: item['deleted_at'])
    assert api.restore(older['id'])['data']['status'] == 'RESTORED'
    middle = settled(world)
    assert api.detail(plan)['content'] == BY_KEY['garden-plan']['user'] and api.detail(done)['content'] == ''
    assert counts(middle)['diary'] == {'entries': 1, 'comments': 0, 'days_to_write_again': 1}

    restore_all(api)
    final = settled(world)
    assert comparable(final) == comparable(with_diary), differences(comparable(with_diary), comparable(final))


@pytest.mark.parametrize('assessed', [False, True], ids=['batch waiting', 'batch assessed'])
def test_an_imported_old_letter_leaves_the_relationship_batches_too(tmp_path, assessed):
    """Imported letters wait in a queue of five for one relationship assessment, with both texts of each pair."""
    backend = Backend(tmp_path / 'app', script_for(), SITE)
    try:
        backend.wait_ready()
        api = backend.api
        letters = import_old_letters(backend, judge=assessed)
        old = OLD_LETTERS[0]
        letter = letters[old['user'][:6]]
        before = api.snapshot()
        assert set(where(before, old['user'])) == {'archive.legacy_letters', 'archive.legacy_letters_fts',
                                                   'imported_relationship.batches', 'originals.chunks', 'originals.originals'}

        assert api.delete(letter, 'user')['data']['status'] == 'DELETED'
        mine = settled(SimpleNamespace(api=api))
        assert where(mine, old['user']) == [], 'my old letter is still stored'
        assert [o['speaker'] for o in api.originals(query='黑胶')] == ['linli']
        # her reply stays in the archive and the index; in the queue it stays only where it was already assessed
        assert {'archive.legacy_letters', 'originals.originals'} <= set(where(mine, old['reply']))
        assert ('imported_relationship.batches' in where(mine, old['reply'])) is assessed

        assert api.delete(letter, 'reply')['data']['status'] == 'DELETED'
        both = settled(SimpleNamespace(api=api))
        assert where(both, old['reply']) == [] and where(both, old['user']) == []
        other = OLD_LETTERS[1]
        assert 'imported_relationship.batches' in where(both, other['user']), 'the other old letter is untouched'

        if not assessed:
            # the relationship assessment runs later: it never sees the deleted letter
            (backend.root / 'history-judge').write_text('on', encoding='utf-8')
            backend.restart()
            api = backend.api
            wait_for_history_batches(backend, 'done')
            events = settled(SimpleNamespace(api=api))['databases']['world']['private_world_events']
            (event,) = [e for e in events if e['event_type'] == 'apply_historical_relationship_evidence']
            assert len(json.loads(event['payload_json'])['evidence_refs']) == 1

        restore_all(api)
        final = settled(SimpleNamespace(api=api))
        if assessed:
            assert comparable(final) == comparable(before), differences(comparable(before), comparable(final))
        else:
            # the batch was assessed without the pair after the deletion and cannot take it back: that one record
            # stays as it is, and everything else of the letter is restored
            own = ('archive.', 'originals.')
            assert {k: v for k, v in comparable(final).items() if k.startswith(own)} ==                 {k: v for k, v in comparable(before).items() if k.startswith(own)}
        assert {'archive.legacy_letters', 'originals.originals'} <= set(where(final, old['user']))
        assert {'archive.legacy_letters', 'originals.originals'} <= set(where(final, old['reply']))
        assert backend.blocked_connections == []
    finally:
        backend.stop()
