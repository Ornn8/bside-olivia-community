"""A diary body is paragraphs separated by one blank line, whichever way the writer broke its lines."""
import asyncio
import json

import pytest

from runtime.diary import diary
from runtime.diary.diary import DiaryStore, QUALITY_PROMPT, REVISE_PROMPT, record_failure, tidy_body, validate
from tests.memory.test_diary_lost_replies import DAY_KEY, ENTRY, NOW, ROWS, Scripted, fail_once, reopen, run

SENTENCES = [
    '今天他说下周三要考乐理，我听出他有点紧张。',
    '我嘴上说没事，心里却一直在想这件事。',
    '后来他又发来一句：“还是有点怕。”',
    '我愣了一下，回他：“我陪你复习呀。”',
    '睡前我又把那句话看了两遍。',
    '窗外的风很大，猫趴在我脚边。',
    '我想起他上次说过的话，忽然有点想笑。',
    '他说转调总是记不住，我就在心里替他记了一遍。',
    '后来他说困了，我也没再多说什么。',
    '关灯之前我又看了一眼手机，屏幕已经暗了。',
    '明天他要是还紧张，我就再陪他过一遍。',
    '这样想着，我慢慢睡着了。',
]
MESSAGES = [{'user': '下周三我要考乐理', 'linli': '我陪你复习呀', 'time': '20:00', 'channel': 'qq'}]


def text(value):
    return ''.join(value.split())


def paragraphs(value):
    return value.split('\n\n')


# --- the rules ------------------------------------------------------------------------------------------------------

def test_a_line_break_after_every_sentence_becomes_a_few_paragraphs():
    body = tidy_body('\n'.join(SENTENCES))
    found = paragraphs(body)
    assert 2 <= len(found) <= 5 and all('\n' not in part for part in found)
    assert all(len(part) >= 40 for part in found)
    assert text(body) == text(''.join(SENTENCES))              # nothing added, nothing lost


def test_a_blank_line_after_every_sentence_is_the_same_mistake_and_is_mended_the_same_way():
    body = tidy_body('\n\n'.join(SENTENCES))
    assert 2 <= len(paragraphs(body)) <= 5 and text(body) == text(''.join(SENTENCES))


def test_a_list_of_sentences_as_the_body_is_mended_too():
    entry = validate({**ENTRY, 'body': SENTENCES}, MESSAGES)
    assert 2 <= len(paragraphs(entry['body'])) <= 5


def test_hard_wrapped_paragraphs_keep_their_blank_lines_and_lose_the_wraps():
    first = ['今天他说下周三要考乐理，我听出他', '有点紧张，嘴上说没事，心里却一', '直在想这件事。']
    second = ['睡前我又把那句话看了两遍，', '窗外的风很大，猫趴在我脚边。']
    body = tidy_body('\n'.join(first) + '\n\n' + '\n'.join(second))
    assert paragraphs(body) == [''.join(first), ''.join(second)]


def test_a_body_that_already_has_its_paragraphs_is_unchanged_and_the_rule_is_stable():
    body = '\n\n'.join(''.join(SENTENCES[start:start + 4]) for start in (0, 4, 8))
    assert tidy_body(body) == body and tidy_body(tidy_body('\n'.join(SENTENCES))) == tidy_body('\n'.join(SENTENCES))


def test_long_lines_are_paragraphs_even_when_only_one_line_break_separates_them():
    one, two = ''.join(SENTENCES[:4]), ''.join(SENTENCES[4:8])
    assert tidy_body(one + '\n' + two) == one + '\n\n' + two


def test_a_short_note_and_empty_text_are_left_alone():
    assert tidy_body('今天很安静。') == '今天很安静。' and tidy_body('') == '' and tidy_body(None) == ''


def test_windows_line_breaks_are_read_like_the_others():
    assert tidy_body('\r\n'.join(SENTENCES)) == tidy_body('\n'.join(SENTENCES))


def test_two_speakers_lines_stay_apart_but_a_quote_in_the_middle_of_a_sentence_is_joined():
    spoken = '“你吃饭了吗？”\n“还没，刚忙完。”'
    assert paragraphs(tidy_body('他一直在等我。\n\n' + spoken + '\n\n后来我们都笑了。')) == [
        '他一直在等我。', '“你吃饭了吗？”', '“还没，刚忙完。”', '后来我们都笑了。']
    assert tidy_body('他说：\n“我陪你。”\n我就笑了。\n\n他又说了一句。') == '他说：“我陪你。”我就笑了。\n\n他又说了一句。'


def test_a_quotation_is_never_cut_across_two_paragraphs():
    quote = '“' + '我一直在想你说过的那句话，' * 12 + '真的。”'
    body = tidy_body('\n'.join([*SENTENCES[:3], quote, *SENTENCES[3:6]]))
    assert any(quote in part for part in paragraphs(body))


def test_english_lines_are_joined_with_a_space():
    lines = ['He said he was nervous about the exam.', 'I told him it was fine.', 'I kept thinking about it though.',
             'Before bed I read his message twice.']
    assert tidy_body('\n'.join(lines)) == ' '.join(lines)


# --- where it is applied --------------------------------------------------------------------------------------------

def test_a_written_entry_is_stored_as_paragraphs_from_the_draft_and_from_the_edit(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    broken = {**ENTRY, 'body': '\n'.join(SENTENCES)}
    assert run(store, Scripted(broken, {'narrator': 'linli'}, broken, {'narrator': 'linli'})) == 'written'
    body = store.entry(DAY_KEY)['body']
    assert 2 <= len(paragraphs(body)) <= 5 and all('\n' not in part for part in paragraphs(body))


def test_an_entry_stored_before_the_fix_is_shown_and_remembered_as_paragraphs(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    with store._db() as db:
        db.execute("INSERT INTO diary_entries (day,written_at,title,mood,body,facts,model,short) VALUES "
                   "('2026-10-06','2026-10-07T00:00:00+00:00','t','m',?,'[]','m',0)", ('\n'.join(SENTENCES),))
    shown = store.entry('2026-10-06')['body']
    assert 2 <= len(paragraphs(shown)) <= 5 and '\n' not in paragraphs(shown)[0]
    assert '\n' not in store.page()['entries'][0]['excerpt'][:40]
    assert '\n' not in store.recent('2026-10-07')[0]['body'][:40]
    assert '\n' not in json.loads(store.context(diary.datetime(2026, 10, 8, 4, tzinfo=diary.SHANGHAI)))['recent_entries'][0]['body'][:40]


def test_the_writer_is_told_how_to_break_its_paragraphs():
    for prompt in (QUALITY_PROMPT, REVISE_PROMPT):
        assert '空一行' in prompt
    assert '不要一句话一行' in QUALITY_PROMPT


# --- replies that could not be read before ---------------------------------------------------------------------------

def test_a_reply_with_raw_line_breaks_inside_the_json_strings_is_read():
    body = '\n\n'.join([''.join(SENTENCES[:4]), ''.join(SENTENCES[4:8])])
    reply = '{"title": "乐理", "mood": "心疼", "body": "%s", "facts": []}' % body
    value = diary._draft_value(reply, (60, 900))
    assert value['body'] == body


def test_quote_marks_inside_the_body_that_were_not_escaped_do_not_cut_it_short():
    body = ''.join(SENTENCES[:4]) + '他说"还是有点怕"，我没有笑他。' + ''.join(SENTENCES[4:8])
    reply = '```json\n{"title": "乐理", "mood": "心疼", "body": "%s", "facts": []}\n```\n希望你喜欢。' % body
    value = diary._draft_value(reply, (60, 900))
    assert value['body'] == body


# --- who is asked after a failure -----------------------------------------------------------------------------------

def test_a_writer_that_fails_twice_in_any_way_hands_the_day_to_another_writer(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    record_failure(store, DAY_KEY, fail_once(store, Scripted(TimeoutError())))
    assert store.failed_models(DAY_KEY) == set()                  # the first time it is asked again
    reopen(store)
    record_failure(store, DAY_KEY, fail_once(store, Scripted(TimeoutError())))
    assert store.failed_models(DAY_KEY) == {'claude-opus-5-5'}
    reopen(store)
    writer = Scripted(ENTRY, {'narrator': 'linli'})
    assert run(store, writer) == 'written' and writer.models[0] == 'deepseek-flash'


def test_the_fourth_reopen_key_gives_a_day_lost_to_unreadable_replies_two_more_tries_once(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    for _ in range(diary.MAX_ATTEMPTS):
        store.failed(DAY_KEY, 'DIARY_NOT_JSON')
    with store._db() as db:
        db.executemany('INSERT INTO diary_settings VALUES (?,?)', [(key, '1') for key, _ in diary.RETRY_ONCE_KEYS[:-1]])
    assert not store.may_attempt(DAY_KEY)
    assert store.retry_rejected([DAY_KEY]) == [DAY_KEY]
    with store._db() as db:
        db.execute('UPDATE diary_attempts SET last_at=0')
    assert store.may_attempt(DAY_KEY) and store.retry_rejected([DAY_KEY]) == []


# --- a month's memoir lost to the same unreadable replies ------------------------------------------------------------

MONTH = '2026-10'


def keys_before_the_fourth(store):
    with store._db() as db:
        db.executemany('INSERT INTO diary_settings VALUES (?,?)', [(key, '1') for key, _ in diary.RETRY_ONCE_KEYS[:-1]])


def test_the_fourth_reopen_key_gives_a_memoir_lost_to_unreadable_replies_two_more_tries_once(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    keys_before_the_fourth(store)
    store.failed(MONTH, 'DIARY_NOT_JSON')
    store.failed(MONTH, 'DIARY_NOT_JSON')                            # two tries used on Opus, the third still open
    for _ in range(diary.MAX_ATTEMPTS):
        store.failed('2026-09', 'ProviderEmptyResponse')            # all three used up
    assert not store.may_attempt('2026-09')
    assert sorted(store.retry_rejected([DAY_KEY])) == ['2026-09', MONTH]
    assert store.attempts(MONTH) == diary.MAX_ATTEMPTS - 2 and store.attempts('2026-09') == diary.MAX_ATTEMPTS - 2
    assert store.may_attempt(MONTH) and store.may_attempt('2026-09')
    assert store.retry_rejected([DAY_KEY]) == []                     # the marker is written: nothing is reopened twice
    store.failed(MONTH, 'DIARY_NOT_JSON')
    store.failed(MONTH, 'DIARY_NOT_JSON')
    assert not store.may_attempt(MONTH)                              # a memoir that fails again stays closed


def test_a_memoir_with_an_entry_or_lost_to_something_else_is_left_alone(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    keys_before_the_fourth(store)
    for _ in range(diary.MAX_ATTEMPTS):
        store.failed('2026-07', 'PermissionError')
    store.deferred('2026-06', 'ProviderUnavailable')                 # nothing reached a service: no try was used
    store.save('2026-08', {'title': 't', 'mood': 'm', 'body': 'b', 'facts': []}, now=NOW, model='claude-opus-5-5',
               short=False, style='prose', photo=None)
    for _ in range(diary.MAX_ATTEMPTS):
        store.failed('2026-08', 'DIARY_NOT_JSON')                    # tries recorded after the entry was kept
    assert store.retry_rejected([DAY_KEY]) == []
    assert store.attempts('2026-07') == diary.MAX_ATTEMPTS and store.attempts('2026-06') == 0
    assert not store.may_attempt('2026-07')


def test_the_memoir_asked_again_after_the_reopen_is_written(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    keys_before_the_fourth(store)
    rows = [{**row, 'life_received_at': row['life_received_at'].replace('2026-10', '2026-09')} for row in ROWS]
    for _ in range(diary.MAX_ATTEMPTS):
        store.failed('2026-09', 'DIARY_NOT_JSON', model='claude-opus-5-5')
    assert not store.may_attempt('2026-09')
    assert diary.due_days(store, NOW) is not None and store.may_attempt('2026-09')
    writer = Scripted(ENTRY, {'narrator': 'linli'}, ENTRY, {'narrator': 'linli'}, model='deepseek-flash')
    outcome = asyncio.run(diary.write_month(store, writer, '2026-09', rows, persona='p', now=NOW,
                                            offered=frozenset({'deepseek-flash'})))
    assert outcome == 'written' and store.entry('2026-09') is not None
