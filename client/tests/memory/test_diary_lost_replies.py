"""A paid writer reply that cannot be made into an entry: what is kept, what is rescued, and who is asked next."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from original_client_relay_api import RELAY_BASE
from runtime.diary import diary
from runtime.diary.diary import DiaryStore, record_failure, write_day, write_month
from tests.memory.test_diary import DAY, ENTRY, NOW, ROWS

DAY_KEY = '2026-10-07'
BODY = '你说下周三要考乐理，我听出你有点紧张，所以一直想陪你把转调再过一遍。' * 8
REFUSAL = '抱歉，我无法按这个要求写这篇日记。这类内容我不能协助完成，请换一个话题。' + '补充说明。' * 120


class Scripted:
    """Answers each call with the next scripted text; remembers the model every call was pinned to."""

    def __init__(self, *texts, model='claude-opus-5-5'):
        self.texts, self.models, self.calls = list(texts), [], []
        self.config = SimpleNamespace(model=model, base_url=RELAY_BASE)

    async def complete_structured_scoped(self, messages, **kwargs):
        from runtime.model_routes import routed_model
        self.calls.append(kwargs['request_id'])
        self.models.append(routed_model(RELAY_BASE, self.config.model))
        text = self.texts.pop(0)
        if isinstance(text, Exception):
            raise text
        return SimpleNamespace(text=text if isinstance(text, str) else json.dumps(text, ensure_ascii=False))


def run(store, writer, offered=frozenset({'deepseek-flash', 'gemini-4-argon', 'claude-opus-5-5'}), day=DAY_KEY):
    return asyncio.run(write_day(store, writer, day, ROWS, persona='p', now=NOW, offered=offered))


def fail_once(store, writer, **kwargs):
    with pytest.raises(Exception) as caught:
        run(store, writer, **kwargs)
    return caught.value


def reopen(store, day=DAY_KEY):
    with store._db() as db:
        db.execute('UPDATE diary_attempts SET last_at=0 WHERE day=?', (day,))


# --- reasons and diagnostics ------------------------------------------------------------------------------------------

def test_a_refusal_stores_the_reason_the_writer_and_the_start_of_the_reply(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    error = fail_once(store, Scripted(REFUSAL))
    assert str(error) == 'DIARY_REFUSED'
    assert record_failure(store, DAY_KEY, error) == 'DIARY_REFUSED'
    kept = store.kept_replies(DAY_KEY)
    assert len(kept) == 1 and kept[0]['code'] == 'DIARY_REFUSED' and kept[0]['model'] == 'claude-opus-5-5'
    assert kept[0]['reply_chars'] == len(REFUSAL) and len(kept[0]['reply_head']) == 400
    assert kept[0]['reply_head'] == REFUSAL[:400]
    assert store.failed_models(DAY_KEY) == {'claude-opus-5-5'}
    with store._db() as db:
        row = db.execute('SELECT attempts,reason FROM diary_attempts').fetchone()
    assert tuple(row) == (1, 'DIARY_REFUSED')


@pytest.mark.parametrize('reply, code', [
    ('{"title": "x", "mood": "y"}', 'DIARY_ENTRY_EMPTY'),
    ('[1, 2]', 'DIARY_ENTRY_INVALID'),
    ({'title': 'x', 'mood': 'y', 'body': '   ', 'facts': []}, 'DIARY_ENTRY_EMPTY'),
    ('今天好像没什么好写的', 'DIARY_NOT_JSON'),
])
def test_other_unusable_replies_are_named(tmp_path, reply, code):
    store = DiaryStore(tmp_path / 'diary.db')
    error = fail_once(store, Scripted(reply))
    assert record_failure(store, DAY_KEY, error) == code
    assert store.kept_replies(DAY_KEY)[0]['code'] == code


def test_the_diagnostic_event_carries_code_and_writer_but_never_the_text(tmp_path):
    import local_server
    from runtime.diagnostics.support_bundle import _project_tail_record
    store = DiaryStore(tmp_path / 'diary.db')
    error = fail_once(store, Scripted(REFUSAL))
    fields = local_server._diary_failure_fields(error)
    assert fields == {'writer_model': 'claude-opus-5-5', 'reply_chars': len(REFUSAL)}
    projected = local_server._runtime_diagnostic_record('diary_write_failed', {'error_code': 'DIARY_REFUSED', **fields})
    assert projected == {'event': 'diary_write_failed', 'error_code': 'DIARY_REFUSED',
                         'writer_model': 'claude-opus-5-5', 'reply_chars': len(REFUSAL)}
    assert _project_tail_record(projected, runtime=True) == projected
    hostile = local_server._runtime_diagnostic_record(
        'diary_write_failed', {'error_code': 'DIARY_REFUSED', 'writer_model': '抱歉，我无法', 'reply_chars': -1,
                               'reply_head': REFUSAL})
    assert hostile == {'event': 'diary_write_failed', 'error_code': 'DIARY_REFUSED'}


def test_network_and_timeout_failures_keep_the_model_and_store_nothing(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    error = fail_once(store, Scripted(TimeoutError()))
    assert record_failure(store, DAY_KEY, error) == 'TimeoutError'
    assert store.failed_models(DAY_KEY) == set() and store.kept_replies(DAY_KEY) == []


# --- who is asked next ------------------------------------------------------------------------------------------------

def test_the_next_try_after_a_refusal_goes_to_another_writer_and_is_not_immediate(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    first = Scripted(REFUSAL)
    record_failure(store, DAY_KEY, fail_once(store, first))
    assert not store.may_attempt(DAY_KEY)                       # no extra paid call right away
    assert asyncio.run(write_day(store, first, DAY_KEY, ROWS, persona='p', now=NOW)) == 'skipped'
    assert len(first.calls) == 1
    reopen(store)
    second = Scripted(ENTRY, {'narrator': 'linli'})
    assert run(store, second) == 'written'
    assert first.models == ['claude-opus-5-5'] and second.models[0] == 'deepseek-flash'
    assert store.entry(DAY_KEY)['title'] == ENTRY['title']
    assert store.kept_replies(DAY_KEY) == []                    # the day is written: the note goes
    with store._db() as db:
        assert db.execute("SELECT model FROM diary_entries").fetchone()[0] == 'deepseek-flash'


def test_a_second_failure_moves_on_to_the_next_writer_and_then_stays_put(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    models = []
    for _ in range(3):
        writer = Scripted(REFUSAL)
        record_failure(store, DAY_KEY, fail_once(store, writer))
        models.append(writer.models[0])
        reopen(store)
    assert models == ['claude-opus-5-5', 'deepseek-flash', 'gemini-4-argon']
    assert store.failed_models(DAY_KEY) == {'claude-opus-5-5', 'deepseek-flash', 'gemini-4-argon'}
    store.failed(DAY_KEY, 'x')
    assert not store.may_attempt(DAY_KEY)                       # MAX_ATTEMPTS used up


def test_without_an_offered_fallback_the_users_model_is_kept(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    record_failure(store, DAY_KEY, fail_once(store, Scripted(REFUSAL)))
    reopen(store)
    for offered in (None, frozenset(), frozenset({'claude-opus-5-5'})):
        writer = Scripted(REFUSAL)
        fail_once(store, writer, offered=offered)
        assert writer.models == ['claude-opus-5-5']
        reopen(store)

    async def unreadable():
        raise OSError('no list')
    writer = Scripted(REFUSAL)
    fail_once(store, writer, offered=unreadable)
    assert writer.models == ['claude-opus-5-5']


def test_a_timeout_retries_the_same_model(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    record_failure(store, DAY_KEY, fail_once(store, Scripted(TimeoutError())))
    reopen(store)
    writer = Scripted(ENTRY, {'narrator': 'linli'})
    assert run(store, writer) == 'written' and writer.models[0] == 'claude-opus-5-5'


def test_a_user_on_the_fallback_writer_moves_to_the_other_one(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    record_failure(store, DAY_KEY, fail_once(store, Scripted(REFUSAL, model='deepseek-flash')))
    reopen(store)
    writer = Scripted(ENTRY, {'narrator': 'linli'}, model='deepseek-flash')
    run(store, writer)
    assert writer.models[0] == 'gemini-4-argon'


def test_another_endpoint_is_never_switched(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    writer = Scripted(REFUSAL)
    writer.config.base_url = 'https://example.invalid/v1'
    record_failure(store, DAY_KEY, fail_once(store, writer))
    reopen(store)
    again = Scripted(REFUSAL)
    again.config.base_url = 'https://example.invalid/v1'
    fail_once(store, again)
    assert again.models == ['claude-opus-5-5']


def test_memoirs_use_the_same_fallback(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    rows = [{**row, 'life_received_at': row['life_received_at'].replace('2026-10', '2026-09')} for row in ROWS]
    writer = Scripted(REFUSAL)
    with pytest.raises(ValueError) as caught:
        asyncio.run(write_month(store, writer, '2026-09', rows, persona='p', now=NOW, offered=frozenset({'deepseek-flash'})))
    record_failure(store, '2026-09', caught.value)
    assert store.failed_models('2026-09') == {'claude-opus-5-5'}


# --- tolerant reading -------------------------------------------------------------------------------------------------

FENCED = '好的，这是今天的日记：\n```json\n' + json.dumps(
    {'title': '下周三的乐理', 'mood': '心疼', 'body': BODY, 'facts': []}, ensure_ascii=False) + '\n```\n希望你喜欢。'
LABELLED = '好的，这是今天的日记：\n\n标题：下周三的乐理\n心情：心疼\n\n' + BODY + '\n\n' + BODY[:60]
MARKDOWN = '# 下周三的乐理\n\n**心情**：心疼\n\n正文：' + BODY
BROKEN_JSON = ('{"title": "下周三的乐理", "mood": "心疼", "body": "' + BODY +
               '", "facts": [{"kind": "promise", "text": "x", "quote": "unterminated')


@pytest.mark.parametrize('reply', [FENCED, LABELLED, MARKDOWN, BROKEN_JSON])
def test_a_wrapped_or_plain_entry_is_accepted_without_a_second_call(tmp_path, reply):
    store = DiaryStore(tmp_path / 'diary.db')
    writer = Scripted(reply, {'narrator': 'linli'})
    assert run(store, writer) == 'written'
    entry = store.entry(DAY_KEY)
    assert entry['title'] == '下周三的乐理' and BODY in entry['body']
    assert entry['facts'] if 'facts' in entry else True
    assert writer.calls[0] == 'diary:2026-10-07' and len(writer.models) <= 3
    assert store.kept_replies(DAY_KEY) == []


def test_labels_alone_make_title_and_mood(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    run(store, Scripted(LABELLED, {'narrator': 'linli'}))
    with store._db() as db:
        row = db.execute('SELECT title,mood FROM diary_entries').fetchone()
    assert tuple(row) == ('下周三的乐理', '心疼')


@pytest.mark.parametrize('reply', [
    REFUSAL,
    '作为AI语言模型，' + BODY,
    '标题：x\n心情：y\n\n' + '抱歉，我无法写这个。' + BODY,
    '好的。\n\n' + '短短一句。',
    '```json\n{"title": "a"}\n```',
])
def test_refusals_prose_about_the_model_and_scraps_are_never_taken_for_a_diary(tmp_path, reply):
    store = DiaryStore(tmp_path / 'diary.db')
    with pytest.raises(ValueError):
        run(store, Scripted(reply))
    assert store.entry(DAY_KEY) is None


def test_a_diary_that_mentions_sorry_or_cannot_is_still_a_diary(tmp_path):
    body = '无法入睡的夜里，我又想起他说的话，对不起三个字在心里转了很久，抱歉的人其实是我。' * 8
    store = DiaryStore(tmp_path / 'diary.db')
    assert run(store, Scripted('标题：无法入睡\n心情：想念\n\n' + body, {'narrator': 'linli'})) == 'written'
    assert body in store.entry(DAY_KEY)['body']


# --- unchanged behaviour ----------------------------------------------------------------------------------------------

def test_clean_json_is_read_exactly_as_before(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    writer = Scripted(ENTRY, {'narrator': 'linli'})
    assert run(store, writer) == 'written'
    assert writer.models[0] == 'claude-opus-5-5'
    with store._db() as db:
        facts = json.loads(db.execute('SELECT facts FROM diary_entries').fetchone()[0])
    assert {f['kind'] for f in facts} == {'user_event', 'anniversary'}


@pytest.mark.real_narrator_check
def test_told_from_his_side_still_discards_and_counts_the_writer(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    writer = Scripted(ENTRY, {'narrator': 'him'})
    error = fail_once(store, writer)
    assert str(error) == 'DIARY_TOLD_FROM_HIS_SIDE'
    assert record_failure(store, DAY_KEY, error) == 'DIARY_TOLD_FROM_HIS_SIDE'
    assert store.failed_models(DAY_KEY) == {'claude-opus-5-5'}


# --- old databases ----------------------------------------------------------------------------------------------------

def test_an_old_database_gains_the_new_columns_and_keeps_its_rows(tmp_path):
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE diary_attempts (day TEXT PRIMARY KEY, attempts INTEGER NOT NULL, last_at REAL NOT NULL, reason TEXT);
            INSERT INTO diary_attempts VALUES ('2026-10-05', 2, 12.0, 'ValueError');
            INSERT INTO diary_attempts VALUES ('2026-10-06', 3, 13.0, 'EMPTY_DAY');
            CREATE TABLE diary_entries (day TEXT PRIMARY KEY, written_at TEXT NOT NULL, title TEXT NOT NULL,
                mood TEXT NOT NULL, body TEXT NOT NULL, facts TEXT NOT NULL, model TEXT,
                short INTEGER NOT NULL DEFAULT 0, seen INTEGER NOT NULL DEFAULT 0);''')
    store = DiaryStore(path)
    DiaryStore(path)                                            # opening it again is safe
    with store._db() as db:
        columns = {row[1] for row in db.execute('PRAGMA table_info(diary_attempts)')}
        assert 'failed_models' in columns
        assert db.execute('SELECT attempts,reason FROM diary_attempts WHERE day=?', ('2026-10-05',)).fetchone()[:] == (2, 'ValueError')
    assert store.failed_models('2026-10-05') == set()
    store.failed('2026-10-05', 'DIARY_REFUSED', model='claude-opus-5-5')
    store.deferred('2026-10-08', 'ProviderUnavailable')
    store.skip('2026-10-09', 'EMPTY_DAY')
    assert store.failed_models('2026-10-05') == {'claude-opus-5-5'}
    with store._db() as db:
        assert db.execute('SELECT attempts FROM diary_attempts WHERE day=?', ('2026-10-05',)).fetchone()[0] == 3
    assert not store.may_attempt('2026-10-05')


def test_kept_replies_are_bounded_and_go_with_a_deleted_day(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    for index in range(diary.REPLY_KEEP_ROWS + 5):
        store.keep_reply('2026-10-%02d' % (index % 28 + 1), 'DIARY_REFUSED', 'm', 'x' * 1000)
    with store._db() as db:
        assert db.execute('SELECT COUNT(*) FROM diary_replies').fetchone()[0] == diary.REPLY_KEEP_ROWS
        assert db.execute('SELECT MAX(length(reply_head)) FROM diary_replies').fetchone()[0] == 400
    store.delete('2026-10-02')
    assert store.kept_replies('2026-10-02') == []
