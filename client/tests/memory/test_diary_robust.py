import asyncio
import json
from types import SimpleNamespace

from runtime.diary.diary import DiaryStore, FORMAT, REVISE_FORMAT, validate, write_day
from tests.memory.test_diary import NOW, ROWS, ENTRY

LONG_FACT = {'kind': 'promise', 'text': '我答应' + '很久很久' * 20, 'date': None, 'quote': '周末一起去看海'}


class Writer:
    config = SimpleNamespace(model='chosen-model')

    def __init__(self, *values):
        self.values, self.calls = list(values), 0

    async def complete_structured_scoped(self, messages, **kwargs):
        self.calls += 1
        return SimpleNamespace(text=json.dumps(self.values.pop(0)))


def test_long_labels_are_clipped_at_a_phrase_and_bad_facts_dropped():
    entry = validate({**ENTRY, 'title': '下周三的乐理考试，我好像比你还要紧张一点点', 'mood': '嘴上嫌弃，心里偷偷软了一下',
                      'facts': [*ENTRY['facts'], LONG_FACT] * 3}, [{'user': '下周三我要考乐理，把它当纪念日', 'linli': ''}])
    assert entry['title'] == '下周三的乐理考试' and entry['mood'] == '嘴上嫌弃'
    assert len(entry['facts']) <= 8 and all(len(fact['text']) <= 60 for fact in entry['facts'])


def test_a_label_without_a_phrase_break_is_cut_to_its_limit():
    entry = validate({**ENTRY, 'mood': '既开心又有点舍不得他走'}, [])
    assert entry['mood'] == '既开心又有点舍不'


def test_the_writer_is_not_told_the_local_length_limits():
    for spec in (FORMAT, REVISE_FORMAT):
        assert 'maxLength' not in json.dumps(spec)


def test_an_overlong_draft_is_still_saved_without_another_call(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    draft = {**ENTRY, 'mood': '嘴上嫌弃，心里偷偷软了一下', 'body': '今天的具体画面。' * 35}
    revision = {'title': '改好了，但标题写得太长太长太长了', 'mood': '心疼又好笑，还有点想他', 'body': '改过的画面。' * 45}
    writer = Writer(draft, revision)
    assert asyncio.run(write_day(store, writer, '2026-10-07', ROWS, persona='p', now=NOW)) == 'written'
    saved = store.entry('2026-10-07')
    assert writer.calls == 2 and saved['mood'] == '心疼又好笑' and saved['body'] == revision['body']


def test_unreached_writer_waits_without_using_an_attempt(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    for _ in range(5):
        store.deferred('2026-10-07', 'ProviderUnavailable')
    with store._db() as db:
        assert db.execute('SELECT attempts FROM diary_attempts').fetchone()[0] == 0
    assert not store.may_attempt('2026-10-07')  # backs off for an hour
    with store._db() as db:
        db.execute('UPDATE diary_attempts SET last_at=0')
    assert store.may_attempt('2026-10-07')
    store.failed('2026-10-07', 'ProviderProtocolError')
    with store._db() as db:
        assert db.execute('SELECT attempts FROM diary_attempts').fetchone()[0] == 1


class Recorder(Writer):
    def __init__(self, *values):
        super().__init__(*values)
        self.ids = []

    async def complete_structured_scoped(self, messages, **kwargs):
        self.ids.append(kwargs['request_id'])
        return await super().complete_structured_scoped(messages, **kwargs)


def test_last_weeks_lost_days_are_rewritten_once_with_the_backfill_label(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    with store._db() as db:
        db.executemany('INSERT INTO diary_attempts VALUES (?,?,?,?)', [
            ('2026-10-07', 3, 0, 'ProviderProtocolError'), ('2026-10-06', 3, 0, 'EMPTY_DAY'),
            ('2026-09-20', 3, 0, 'ProviderProtocolError'), ('2026-10-05', 1, 0, 'ProviderUnavailable')])
    store.save('2026-10-05', {**ENTRY, 'facts': []}, now=NOW, model='m', short=False, style='prose', photo=None)
    store.prepare_backfill('2026-10-08')
    assert [store.backfill(day) for day in ('2026-10-07', '2026-10-06', '2026-09-20', '2026-10-05')] == [True, False, False, False]
    assert store.may_attempt('2026-10-07')
    with store._db() as db:
        db.execute("INSERT INTO diary_attempts VALUES ('2026-10-04',3,0,'ProviderProtocolError')")
    store.prepare_backfill('2026-10-08')  # once only
    assert not store.backfill('2026-10-04')
    writer = Recorder({**ENTRY, 'body': '今天的具体画面。' * 35}, {'title': '改好了', 'mood': '暖', 'body': '改过的画面。' * 45})
    assert asyncio.run(write_day(store, writer, '2026-10-07', ROWS, persona='p', now=NOW)) == 'written'
    assert [i.split(':')[0] for i in writer.ids] == ['diary-backfill', 'diary-backfill-revise']
    assert not store.backfill('2026-10-07')
