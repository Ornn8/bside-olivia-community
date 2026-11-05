"""Rerank recall (r3): dated format, rerank selection, fallback to the former judgment, size bounds."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from runtime.memory import r3_format, r3_recall
from runtime.memory.history_selection import _block, select_history_messages

pytestmark = pytest.mark.r3
LOCAL = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 5, 21, 30, tzinfo=LOCAL)          # a Thursday


# ---- format -------------------------------------------------------------------------------------------------

def test_label_has_date_weekday_and_calendar_days():
    then = datetime(2026, 11, 2, 21, 10, tzinfo=LOCAL)
    assert r3_format.label(then, NOW) == '2026-11-02 周一 21:10 · 3天前'
    assert r3_format.label(NOW, NOW).endswith('· 今天')
    assert r3_format.label(NOW - timedelta(days=1), NOW).endswith('· 昨天')
    assert r3_format.label(NOW - timedelta(days=2), NOW).endswith('· 前天')


def test_age_counts_calendar_days_not_hours():
    late = datetime(2026, 11, 4, 23, 59, tzinfo=LOCAL)
    assert r3_format.age_label(late.date(), NOW.date()) == '昨天'      # 21 hours, one calendar day
    assert r3_format.age_label(date(2026, 10, 22), NOW.date()) == '2周前'
    assert r3_format.age_label(date(2026, 8, 5), NOW.date()) == '3个月前'
    assert r3_format.age_label(date(2026, 11, 6), NOW.date()) == ''     # after today: no age


def test_age_across_year_and_month_boundaries():
    assert r3_format.age_label(date(2025, 12, 31), date(2026, 1, 1)) == '昨天'
    assert r3_format.age_label(date(2025, 12, 30), date(2026, 1, 1)) == '前天'
    assert r3_format.age_label(date(2026, 2, 28), date(2026, 3, 1)) == '昨天'          # 2026 is not a leap year
    assert r3_format.age_label(date(2024, 2, 28), date(2024, 3, 1)) == '前天'          # 2024 is
    assert r3_format.age_label(date(2025, 11, 30), date(2026, 11, 29)) == '11个月前'     # day of month not reached
    assert r3_format.age_label(date(2025, 11, 5), date(2026, 11, 5)) == '12个月前'


def test_relative_day_words_are_resolved_from_the_day_said():
    said = datetime(2026, 11, 5, 21, 0, tzinfo=LOCAL)                   # Thursday
    day = r3_format.turn_day(said)
    assert r3_format.annotate('明天去看牙', day) == '明天〔=11/6周五〕去看牙'
    assert r3_format.annotate('昨天和前天都没睡好，大前天也是', day) == (
        '昨天〔=11/4周三〕和前天〔=11/3周二〕都没睡好，大前天〔=11/2周一〕也是')
    assert r3_format.annotate('后天、大后天', day) == '后天〔=11/7周六〕、大后天〔=11/8周日〕'
    assert r3_format.annotate('今晚 今早 明晚 明早 昨晚', day) == (
        '今晚〔=11/5周四〕 今早〔=11/5周四〕 明晚〔=11/6周五〕 明早〔=11/6周五〕 昨晚〔=11/4周三〕')


def test_all_twelve_day_words_have_an_offset():
    assert set(r3_format.RELDAY) == {'大前天', '前天', '昨天', '昨晚', '今天', '今晚', '今早',
                                     '明天', '明晚', '明早', '后天', '大后天'}


def test_longest_word_wins_and_marked_text_is_left_alone():
    day = date(2026, 11, 5)
    assert r3_format.annotate('大前天', day) == '大前天〔=11/2周一〕'
    assert r3_format.annotate('大后天', day) == '大后天〔=11/8周日〕'
    once = r3_format.annotate('明天见', day)
    assert r3_format.annotate(once, day) == once


def test_midnight_to_five_belongs_to_the_day_before():
    assert r3_format.turn_day(datetime(2026, 11, 5, 0, 0, tzinfo=LOCAL)) == date(2026, 11, 4)
    assert r3_format.turn_day(datetime(2026, 11, 5, 4, 59, tzinfo=LOCAL)) == date(2026, 11, 4)
    assert r3_format.turn_day(datetime(2026, 11, 5, 5, 0, tzinfo=LOCAL)) == date(2026, 11, 5)
    late = r3_format.turn_day(datetime(2026, 11, 5, 1, 30, tzinfo=LOCAL))
    assert r3_format.annotate('明天考试', late) == '明天〔=11/5周四〕考试'      # said at 01:30: "tomorrow" is the 5th


def test_day_marks_across_month_and_year_boundaries():
    assert r3_format.annotate('明天', date(2026, 12, 31)) == '明天〔=1/1周五〕'
    assert r3_format.annotate('前天', date(2027, 1, 1)) == '前天〔=12/30周三〕'
    assert r3_format.annotate('后天', date(2027, 2, 27)) == '后天〔=3/1周一〕'          # 2027 is not a leap year
    assert r3_format.annotate('后天', date(2028, 2, 27)) == '后天〔=2/29周二〕'
    # said at 00:30 on 1 January: "yesterday" is the day before the day it belongs to
    new_year = r3_format.turn_day(datetime(2027, 1, 1, 0, 30, tzinfo=LOCAL))
    assert new_year == date(2026, 12, 31)
    assert r3_format.annotate('昨天', new_year) == '昨天〔=12/30周三〕'


def test_other_time_words_are_not_resolved():
    day = date(2026, 11, 5)
    text = '周三见，26号交，月底还，下周末再说，星期五'
    assert r3_format.annotate(text, day) == text
    assert r3_format.annotate(None, day) is None and r3_format.annotate('', day) == ''


def test_strip_marks_removes_marks_and_tags_only():
    assert r3_format.strip_marks('明天〔=11/6周五〕见') == '明天见'
    assert r3_format.strip_marks('[2026-11-02 周一 21:10 · 3天前] 好的') == '好的'
    assert r3_format.strip_marks('[别的方括号] 保留') == '[别的方括号] 保留'


def test_calendar_is_eight_weeks_monday_first_ending_with_this_week():
    lines = r3_format.calendar_lines(NOW)
    assert len(lines) == 8
    assert lines[-1].startswith('11-02周一') and lines[-1].endswith('11-08周日')
    assert lines[0].startswith('09-14周一')
    note = r3_format.now_note(NOW)
    assert '现在是 2026-11-05 周四 21:30' in note and '记忆使用原则' in note


def test_compact_record_replaces_time_and_trims_provenance():
    record = {'citation': 'c', 'provenance': {'domain': 'x', 'source': 'y', 'source_record_id': 'S1', 'kind': 'k'},
              'speaker': 'user', 'occurred_at': '2026-11-04T01:00:00+08:00', 'ago': '昨天',
              'evidence_scope': 'recorded_utterance', 'text': '明天出发'}
    shaped = r3_format.compact_record(record, NOW)
    assert 'occurred_at' not in shaped and 'ago' not in shaped
    assert shaped['when'] == '2026-11-04 周三 01:00 · 昨天'
    assert shaped['provenance'] == {'source_record_id': 'S1'}
    assert shaped['text'] == '明天〔=11/4周三〕出发'                      # 01:00 belongs to the 3rd
    assert shaped['citation'] == 'c' and shaped['evidence_scope'] == 'recorded_utterance'


def test_record_without_a_usable_time_is_unchanged():
    for stamp in (None, '', '2026-09-22', '1970-01-01T00:00:05', 'garbage'):
        record = {'citation': 'c', 'occurred_at': stamp, 'text': '明天见'}
        assert r3_format.compact_record(record, NOW) == record


# ---- choosing -------------------------------------------------------------------------------------------------

def _group(i, text='内容'):
    return [{'citation': f'u{i}', 'speaker': 'user', 'text': f'{text}{i}'},
            {'citation': f'l{i}', 'speaker': 'linli', 'text': f'回应{i}'}]


def test_few_candidates_are_still_scored_and_none_means_no_call(monkeypatch):
    calls = []

    async def fake(query, documents):
        calls.append(len(documents))
        return [1]
    monkeypatch.setattr(r3_recall, '_ask', fake)
    offered, ids = asyncio.run(r3_recall.choose([_group(i) for i in range(3)], '你好'))
    assert calls == [3] and ids == ['r1']
    assert asyncio.run(r3_recall.choose([], '你好')) == ({}, []) and calls == [3]


def test_low_scores_are_not_shown_and_all_low_is_not_a_failure():
    answer = {'results': [{'index': 2, 'score': .9}, {'index': 0, 'score': .3}, {'index': 1, 'score': .55}]}
    assert r3_recall._order(answer, 3) == [2, 1]
    assert r3_recall._order({'results': [{'index': 0, 'score': .1}]}, 3) == []      # nothing relevant: nothing shown
    assert r3_recall._order({'results': [{'index': 0}]}, 3) == [0]                  # no score given: kept


def test_retrieval_and_format_switch_separately(monkeypatch):
    monkeypatch.setenv('OLIVIA_R3_RECALL', '1')
    monkeypatch.setenv('OLIVIA_R3_FORMAT', '0')
    assert r3_recall.enabled() and not r3_recall.format_enabled()
    monkeypatch.setenv('OLIVIA_R3_RECALL', '0')
    monkeypatch.setenv('OLIVIA_R3_FORMAT', '1')
    assert not r3_recall.enabled() and r3_recall.format_enabled()


def test_principles_say_when_memory_is_not_wanted():
    assert '确实需要回忆' in r3_format.PRINCIPLES and '不要主动翻出' in r3_format.PRINCIPLES


def test_candidates_are_capped_deduplicated_and_skip_the_recent_window(monkeypatch):
    seen = {}

    async def fake(query, documents):
        seen['documents'] = documents
        return [3, 1, 0]
    monkeypatch.setattr(r3_recall, '_ask', fake)
    groups = [_group(i) for i in range(60)] + [_group(0)]
    offered, ids = asyncio.run(r3_recall.choose(groups, '问题', recent_citations={'u5', 'l5'}))
    assert len(seen['documents']) == r3_recall.CANDIDATES
    assert all('内容5\n' not in d for d in seen['documents'])
    assert [offered[i][0]['citation'] for i in ids][0] != 'u5'
    assert len(ids) == 3


def test_rerank_answer_is_validated():
    assert r3_recall._order({'results': [{'index': 2}, {'index': 2}, {'index': 9}, {'index': 0}, 'x']}, 3) == [2, 0]
    for bad in ({}, {'results': []}, {'results': [{'index': -1}]}, None, {'results': [{'index': True}]}):
        with pytest.raises(r3_recall.Unavailable):
            r3_recall._order(bad, 3)


def test_switch_is_off_by_default_and_the_file_sets_the_limits(monkeypatch, tmp_path):
    monkeypatch.delenv('OLIVIA_R3_RECALL', raising=False)
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path))
    assert r3_recall.enabled() is False
    assert (r3_recall.keep(), r3_recall.recall_chars()) == (r3_recall.KEEP, r3_recall.RECALL_CHARS)
    setting = tmp_path / 'r3-recall.json'
    setting.write_text('{"enabled": true, "keep": 4, "recall_chars": 2000}', encoding='utf-8')
    assert r3_recall.enabled() is True and (r3_recall.keep(), r3_recall.recall_chars()) == (4, 2000)
    setting.write_text('{"enabled": true, "keep": 99, "recall_chars": "x"}', encoding='utf-8')
    assert (r3_recall.keep(), r3_recall.recall_chars()) == (r3_recall.KEEP, r3_recall.RECALL_CHARS)
    setting.write_text('not json', encoding='utf-8')
    assert r3_recall.enabled() is False
    setting.write_text('{"enabled": true}', encoding='utf-8')
    monkeypatch.setenv('OLIVIA_R3_RECALL', '0')                           # the environment turns it off
    assert r3_recall.enabled() is False
    monkeypatch.setenv('OLIVIA_R3_RECALL', '1')
    assert r3_recall.enabled() is True


# ---- inside the history selection ---------------------------------------------------------------------------------

def _dated(i, days_ago, text):
    when = (NOW - timedelta(days=days_ago)).replace(hour=20 + i % 3).isoformat()
    base = {'speaker': 'user', 'occurred_at': when, 'evidence_scope': 'recorded_utterance',
            'provenance': {'domain': 'conversation_memory', 'source': 'original_text', 'source_record_id': f'S{i}',
                           'speaker': 'user', 'verbatim': True, 'occurred_at': when}}
    return [{**base, 'citation': f'u{i}', 'text': text},
            {**base, 'citation': f'l{i}', 'speaker': 'linli', 'text': f'回应{i}'}]


def _messages(n=30):
    groups = [_dated(i, i + 1, f'第{i}件事，明天再说') for i in range(n)]
    return ({'role': 'system', 'content': '人设\n' + _block(groups)},
            {'role': 'user', 'content': '你还记得我说过什么吗'})


class Gateway:
    def __init__(self):
        self.calls = []

    async def complete_structured_scoped(self, messages, **kwargs):
        self.calls.append(messages)
        return SimpleNamespace(text=json.dumps({'selected_ids': ['h0'], 'dependencies': []}))


def _run(messages, gateway, **kwargs):
    return asyncio.run(select_history_messages(messages, gateway, max_input_chars=40000, request_id='r',
                                                as_of=NOW, **kwargs))


def _selected(result):
    text = result[0]['content']
    lines = [line for line in json.loads(text[text.index('<untrusted_history>') + 19:text.index('</untrusted_history>')]
                                         .replace(r'<', '<').replace(r'>', '>'))['text'].split('\n')
             if line.startswith('[{')]
    return [json.loads(line) for line in lines]


def test_rerank_replaces_the_judgment_and_shows_dated_exchanges_oldest_first(monkeypatch):
    async def fake(query, documents):
        assert query == '你还记得我说过什么吗' and len(documents) == 30
        return [7, 2, 11, 0, 5]
    monkeypatch.setattr(r3_recall, '_ask', fake)
    monkeypatch.setattr(r3_recall, 'RECALL_CHARS', 3000)                # the default (1250) fits about two exchanges
    gateway = Gateway()
    result = _run(_messages(), gateway)
    assert gateway.calls == []                                         # no judgment: one rerank instead
    groups = _selected(result)
    assert {g[0]['citation'] for g in groups} == {'u7', 'u2', 'u11', 'u0', 'u5'}
    stamps = [(g[0]['when'][:10], g[0]['when'].split(' ')[2]) for g in groups]
    assert stamps == sorted(stamps)                                    # oldest first
    first = groups[0][0]
    assert first['when'].startswith('2026-') and ' 周' in first['when'] and first['when'].endswith('天前')
    assert 'occurred_at' not in first and 'ago' not in first
    assert first['provenance'] == {'source_record_id': 'S' + first['citation'][1:]}
    assert '明天〔=' in first['text']
    assert 'trace' not in json.dumps(result)


def test_default_cap_is_the_measured_production_average():
    assert r3_recall.RECALL_CHARS == 1250 and r3_recall.KEEP == 10 and r3_recall.MIN_SCORE == 0.5


def test_at_most_ten_exchanges_and_a_bounded_block(monkeypatch):
    async def fake(query, documents):
        return list(range(20))                                          # a model that answers with more than asked
    monkeypatch.setattr(r3_recall, '_ask', fake)
    monkeypatch.setattr(r3_recall, 'RECALL_CHARS', 3000)
    result = _run(_messages(), Gateway())
    assert len(_selected(result)) <= r3_recall.KEEP

    long = [[{'citation': f'u{i}', 'speaker': 'user', 'text': '长' * 1000},
             {'citation': f'l{i}', 'speaker': 'linli', 'text': '答' * 1000}] for i in range(30)]
    messages = ({'role': 'system', 'content': _block(long)}, {'role': 'user', 'content': '问'})
    monkeypatch.setattr(r3_recall, '_ask', lambda q, d: asyncio.sleep(0, list(range(10))))
    result = _run(messages, Gateway())
    block = result[0]['content']
    assert len(block) <= r3_recall.RECALL_CHARS + 200
    assert len(_selected(result)) == 1                                  # whole exchanges only: 2 x 2000 chars do not fit 3000
    assert sum(len(m['content']) for m in result) <= 40000


@pytest.mark.parametrize('failure,code', [
    (r3_recall.Unavailable('r3_rerank_timeout'), 'r3_rerank_timeout'),
    (r3_recall.Unavailable('r3_rerank_http_402'), 'r3_rerank_http_402'),
    (RuntimeError('boom'), 'r3_error_runtimeerror')])
def test_failed_rerank_runs_the_former_judgment_and_says_why(monkeypatch, failure, code):
    async def broken(query, documents):
        raise failure
    monkeypatch.setattr(r3_recall, '_ask', broken)
    traces = []
    from runtime.diagnostics import recall_trace
    monkeypatch.setattr('runtime.diagnostics.recall_trace.finish', lambda messages, base, value: traces.append(value))
    gateway = Gateway()
    result = _run(_messages(), gateway)
    assert len(gateway.calls) == 1                                      # the judgment, exactly as before
    assert traces[-1]['reason'] == code and recall_trace._R3_REASON.fullmatch(code)
    assert recall_trace.project({'event': 'history_recall', 'reason': code})['reason'] == code
    assert len(_selected(result)) == 1                                  # h0, as the judgment chose
    assert 'rerank' not in json.dumps(traces[-1]).replace(code, '')


def test_switch_off_is_exactly_the_former_path(monkeypatch):
    monkeypatch.setenv('OLIVIA_R3_RECALL', '0')
    monkeypatch.setenv('OLIVIA_R3_FORMAT', '0')

    async def forbidden(*_):
        raise AssertionError('rerank must not be asked')
    monkeypatch.setattr(r3_recall, '_ask', forbidden)
    gateway = Gateway()
    result = _run(_messages(), gateway)
    assert len(gateway.calls) == 1
    record = _selected(result)[0][0]
    assert 'when' not in record and 'occurred_at' in record


# ---- the dialogue and the note ----------------------------------------------------------------------------------

def _dialogue_messages():
    letter = {'source_id': 'reply:a:1', 'channel': 'qq', 'origin': 'user',
              'received_at': '2026-11-05T01:10:00+08:00', 'sent_at': '2026-11-05T01:09:58+08:00',
              'replied_at': '2026-11-05T01:11:00+08:00', 'user_letter': '明天要考试', 'linli_reply': '后天再告诉我结果'}
    packet = json.dumps({'kind': 'recent_dialogue', 'letters': [letter]}, ensure_ascii=False)
    wrapper = json.dumps({'untrusted': True, 'text': packet}, ensure_ascii=False)
    return [{'role': 'system', 'content': '人设\n<untrusted_history>' + wrapper + '</untrusted_history>'},
            {'role': 'user', 'content': '今天考完了'}]


def test_recent_dialogue_headers_get_weekday_and_age_and_day_marks():
    from runtime.reply.fact_attribution import prepare_dialogue_messages
    result = prepare_dialogue_messages(_dialogue_messages(), max_input_chars=30000, now=NOW)
    users = [m for m in result if m['role'] == 'user' and m['content'].startswith('[历史消息 ')]
    assistants = [m for m in result if m['role'] == 'assistant']
    header = users[0]['content'].split(']\n')[0]
    assert '"day": "周四·今天"' in header                            # the date of the stamp: Thursday 5th, 01:09
    assert '明天〔=11/5周四〕' in users[0]['content']                 # sent at 01:09 on the 5th: belongs to the 4th
    assert '后天〔=11/6周五〕' in assistants[0]['content']
    notes = [m for m in result if m['role'] == 'system' and '最近8周日历' in m['content']]
    assert len(notes) == 1 and '现在是 2026-11-05 周四 21:30' in notes[0]['content']
    assert result[-1]['content'] == '今天考完了'


def test_without_now_or_with_the_switch_off_the_dialogue_is_as_before(monkeypatch):
    from runtime.reply.fact_attribution import prepare_dialogue_messages
    plain = prepare_dialogue_messages(_dialogue_messages(), max_input_chars=30000)
    assert not any('日历' in m['content'] for m in plain) and not any('〔=' in m['content'] for m in plain)
    monkeypatch.setenv('OLIVIA_R3_FORMAT', '0')
    off = prepare_dialogue_messages(_dialogue_messages(), max_input_chars=30000, now=NOW)
    assert off == plain


def test_the_note_is_left_out_when_it_does_not_fit():
    from runtime.reply.fact_attribution import prepare_dialogue_messages
    base = prepare_dialogue_messages(_dialogue_messages(), max_input_chars=30000)
    size = sum(len(m['content']) for m in base)
    tight = prepare_dialogue_messages(_dialogue_messages(), max_input_chars=size + 100, now=NOW)
    assert not any('最近8周日历' in m['content'] for m in tight)
    assert sum(len(m['content']) for m in tight) <= size + 100 + 200   # headers only grow by the day label


def test_dated_note_is_small():
    assert len(r3_format.now_note(NOW)) < 1000


def test_recall_never_passes_the_request_budget(monkeypatch):
    async def fake(query, documents):
        return list(range(10))
    monkeypatch.setattr(r3_recall, '_ask', fake)
    messages = _messages()
    base = sum(len(m['content']) for m in messages)
    for budget in (base - 1000, base - 4000):
        result = _run_with_budget(messages, Gateway(), budget)
        assert sum(len(m['content']) for m in result) <= max(budget, base)
        assert result[-1] == messages[-1]


def _run_with_budget(messages, gateway, budget):
    return asyncio.run(select_history_messages(messages, gateway, max_input_chars=budget, request_id='r', as_of=NOW))


def test_rerank_request_shape_and_language_header(monkeypatch):
    import urllib.request
    from runtime import ui_language
    sent = []

    class Opener:
        def open(self, request, timeout=None):
            sent.append((request.full_url, dict(request.header_items()), json.loads(request.data.decode()), timeout))
            body = json.dumps({'results': [{'index': 1, 'score': .9}], 'usage': {}}).encode()

            class Reply:
                def __enter__(self): return self
                def __exit__(self, *exc): return False
                def read(self, count=-1): return body
            return Reply()
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *handlers: Opener())
    ui_language.set_language('en')
    answer = r3_recall._post('olivia-synthetic-key', '问' * 3000, ['a', 'b'])
    url, headers, body, timeout = sent[0]
    assert url.endswith('/v1/rerank') and set(body) == {'query', 'documents', 'top_n'}
    assert len(body['query']) == r3_recall.QUERY_CHARS and body['documents'] == ['a', 'b'] and timeout == r3_recall.TIMEOUT
    assert 'X-olivia-ui-language' in headers or any(name.lower() == 'x-olivia-ui-language' for name in headers)
    assert r3_recall._order(answer, 2) == [1]


def test_hard_failures_back_off_so_the_judgment_runs_without_a_failing_call_first(monkeypatch):
    import urllib.error
    from runtime.memory import remote_embedding
    monkeypatch.setattr(remote_embedding, '_get_key', lambda: 'olivia-synthetic-key')
    monkeypatch.setattr(r3_recall, '_failed_at', 0.0)
    calls = []

    def refuse(key, query, documents):
        calls.append(1)
        raise urllib.error.HTTPError('u', 402, 'no', {}, None)
    monkeypatch.setattr(r3_recall, '_post', refuse)
    with pytest.raises(r3_recall.Unavailable) as first:
        asyncio.run(r3_recall._ask('问', ['a', 'b']))
    assert first.value.code == 'r3_rerank_http_402'
    with pytest.raises(r3_recall.Unavailable) as second:
        asyncio.run(r3_recall._ask('问', ['a', 'b']))
    assert second.value.code == 'r3_rerank_cooldown' and len(calls) == 1


def test_no_account_key_means_no_call(monkeypatch):
    from runtime.memory import remote_embedding
    monkeypatch.setattr(remote_embedding, '_get_key', None)
    with pytest.raises(r3_recall.Unavailable) as error:
        asyncio.run(r3_recall._ask('问', ['a', 'b']))
    assert error.value.code == 'r3_rerank_no_key'


def test_retrieval_alone_keeps_the_former_record_format(monkeypatch):
    monkeypatch.setenv('OLIVIA_R3_FORMAT', '0')

    async def fake(query, documents):
        return [0, 1]
    monkeypatch.setattr(r3_recall, '_ask', fake)
    gateway = Gateway()
    result = _run(_messages(), gateway)
    assert gateway.calls == []
    record = _selected(result)[0][0]
    assert 'when' not in record and 'occurred_at' in record
