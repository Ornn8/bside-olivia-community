"""A day whose diary was deleted together with a letter is written again from what is left of that day."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from runtime.diary.diary import DiaryStore, MAX_ATTEMPTS, SHANGHAI, due_days, write_day
from runtime.imports.letter_delete import apply_diary, diary_plan

NOW = datetime(2026, 10, 8, 4, 0, tzinfo=SHANGHAI)
DAY = '2026-10-07'
OLD_DAY = '2026-09-01'  # older than the days that are caught up (CATCH_UP_DAYS)
WRITTEN = datetime(2026, 10, 8, 1, 30, tzinfo=SHANGHAI)


def letter(content, reply, day, hour):
    at = datetime.fromisoformat(day).replace(tzinfo=SHANGHAI) + timedelta(hours=hour)
    return {'letter_id': 'l' + at.isoformat(), 'content': content, 'reply_text': reply, 'letter_status': 'COMPLETED',
            'life_received_at': at.isoformat(), 'replied_at': at.isoformat()}


class Writer:
    """A stand-in for the model: the entry it writes quotes every message it was given."""

    def __init__(self):
        self.packets = []
        self.config = SimpleNamespace(model='m')

    async def complete_structured_scoped(self, messages, **kwargs):
        packet = json.loads(messages[1]['content'])
        self.packets.append(packet)
        said = ' / '.join(part for m in packet['messages'] for part in (m['他写给你的'], m['你写给他的']) if part)
        return SimpleNamespace(text=json.dumps({'title': '那天', 'mood': '平静', 'body': '今天的信：' + said, 'facts': []},
                                               ensure_ascii=False))


def written(store, day, body='原来的日记'):
    assert store.save(day, {'title': '原来的', 'mood': '平静', 'body': body, 'facts': []}, now=WRITTEN, model='m', short=False)


def delete_with_diary(store, day):
    plan = diary_plan(store.path, [day])
    apply_diary(store.path, plan)
    return plan


def test_a_day_written_again_comes_first_even_when_older_than_the_caught_up_days(tmp_path):
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    written(store, DAY)
    written(store, OLD_DAY)
    delete_with_diary(store, OLD_DAY)
    delete_with_diary(store, DAY)
    assert store.rewrite_days() == [OLD_DAY, DAY]
    days = due_days(store, NOW)
    assert days[:2] == [OLD_DAY, DAY] and days.count(DAY) == 1 and len(days) == 8  # the week's other days follow
    # without the request the old day would never come back
    assert OLD_DAY not in due_days(DiaryStore(tmp_path / 'other.sqlite3'), NOW)


def test_the_list_empties_when_the_day_is_written_dropped_empty_or_out_of_tries(tmp_path):
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    for day in ('2026-10-01', '2026-10-02', '2026-10-03', '2026-10-04'):
        written(store, day)
        delete_with_diary(store, day)
    assert len(store.rewrite_days()) == 4
    assert store.save('2026-10-01', {'title': 't', 'mood': 'm', 'body': 'b', 'facts': []}, now=WRITTEN, model='m', short=False)
    store.skip('2026-10-02', 'EMPTY_DAY')
    store.delete('2026-10-03')
    for _ in range(MAX_ATTEMPTS):
        store.failed('2026-10-04', 'ValueError')
    assert store.rewrite_days() == []


def test_the_day_is_written_again_from_the_messages_that_are_left(tmp_path):
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    kept = letter('我想听雨', '雨声最安静', DAY, 20)
    deleted = letter('这句话会被删掉', '这句回信也会被删掉', DAY, 9)
    written(store, DAY, body='原来的日记写到了：这句话会被删掉')
    store.checkpoint(DAY, 'draft', {'entry': {'body': '草稿里也有：这句话会被删掉'}})
    delete_with_diary(store, DAY)
    assert store.entry(DAY) is None and store.work(DAY) == (None, None)
    # what the deletion of the letter leaves in the mailbox: the deleted side is empty, the other side stays
    deleted['content'] = ''
    rows = [kept, deleted]

    writer = Writer()
    assert asyncio.run(write_day(store, writer, DAY, rows, persona='p', now=NOW)) == 'written'
    sent = json.dumps(writer.packets, ensure_ascii=False)
    assert '这句话会被删掉' not in sent and '我想听雨' in sent and '这句回信也会被删掉' in sent  # her reply was not deleted
    entry = store.entry(DAY)
    assert '这句话会被删掉' not in entry['body'] and '我想听雨' in entry['body']
    assert store.rewrite_days() == []  # written once: not queued again


def test_a_day_with_nothing_left_stays_without_a_diary(tmp_path):
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    written(store, DAY)
    delete_with_diary(store, DAY)
    only = letter('', '', DAY, 9)
    assert asyncio.run(write_day(store, Writer(), DAY, [only], persona='p', now=NOW)) == 'empty'
    store.skip(DAY, 'EMPTY_DAY')
    assert store.rewrite_days() == [] and store.entry(DAY) is None


def test_restoring_replaces_the_written_again_entry_and_nothing_is_written_after_it(tmp_path):
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    written(store, DAY)
    store.add_comment(DAY, '评论', WRITTEN)
    plan = delete_with_diary(store, DAY)
    assert asyncio.run(write_day(store, Writer(), DAY, [letter('我想听雨', '雨声最安静', DAY, 20)], persona='p', now=NOW)) == 'written'
    assert store.entry(DAY)['body'].startswith('今天的信')
    apply_diary(store.path, plan, restore=True)
    entry = store.entry(DAY)
    assert entry['body'] == '原来的日记' and [c['text'] for c in entry['comments']] == ['评论']
    assert store.rewrite_days() == [] and DAY not in due_days(store, NOW)
    # restoring twice, or deleting twice, changes nothing more
    apply_diary(store.path, plan, restore=True)
    assert store.entry(DAY)['body'] == '原来的日记'
    apply_diary(store.path, plan)
    apply_diary(store.path, plan)
    assert store.entry(DAY) is None and store.rewrite_days() == [DAY]


def test_the_plan_is_empty_when_there_is_no_entry_and_leaves_the_memoir_alone(tmp_path):
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    assert diary_plan(store.path, [DAY]) is None
    written(store, '2026-10')  # the memoir of the month
    assert diary_plan(store.path, [DAY]) is None
    written(store, DAY)
    delete_with_diary(store, DAY)
    assert store.entry('2026-10')['body'] == '原来的日记'
