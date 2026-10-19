"""A written draft is never thrown away for something that can be fitted, whichever model wrote it."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from llm_gateway import (GatewayConfig, GatewayError, GatewayRequestScope, OpenAICompatibleAdapter, ProviderProtocolError,
                         ProviderTimeout, ProviderUnavailable)
from runtime import ui_language
from runtime.diary import diary
from runtime.diary.diary import (DiaryStore, MAX_ATTEMPTS, _clip, due_days, unserved, validate, write_day)
from tests.memory.test_diary import ENTRY, NOW, ROWS

DAY = '2026-10-07'
BODY = '今天的具体画面。' * 35  # 280 characters: inside the length asked for


def fact(kind='user_event', text='对方下周三考乐理', date='2026-10-14', quote='下周三我要考乐理'):
    return {'kind': kind, 'text': text, 'date': date, 'quote': quote}


def fenced(value, before='好的，这是昨晚的日记：\n', after='\n希望你喜欢。'):
    return before + '```json\n' + json.dumps(value, ensure_ascii=False) + '\n```' + after


# What the Sonnet and Opus drafts looked like: labels as long as a sentence, many facts, an optional key left out.
CLAUDE_DRAFT = {
    'title': '他说下周三要考乐理，我嘴上说没事其实一直在想这件事',
    'mood': '有点委屈又有点想他',
    'body': BODY,
    'facts': [fact(), fact('anniversary', '8月26日是我们的纪念日', '2026-08-26', '把它当纪念日'),
              fact('event', '他今天复习了一下午乐理', None, '复习了一下午'),                       # a kind that does not exist
              {'kind': 'plan', 'text': '周末要一起复习', 'quote': '记不住转调'},                  # no date key
              fact('user_event', '他' + '一直都很紧张' * 20, None, '好紧张'),                       # text far over 60
              fact('promise', '我答应陪他', None, ''),                                             # no quote
              fact('promise', '我答应周末陪对方去看海', None, '周末一起去看海'),                   # invented, not said
              *[fact('preference', f'他的小习惯{i}', None, '记不住转调') for i in range(8)]],      # more than eight in all
}


REVISION = {'title': '他的乐理考试，我比他还紧张一点点，也不知道该怎么安慰他', 'mood': '心疼又好笑，还有点想他', 'body': BODY}


class Writer:
    """Hands back raw text, as a model that is not held to the schema does."""
    config = SimpleNamespace(model='claude-sonnet-5-5')

    def __init__(self, *replies):
        self.replies, self.ids = list(replies), []

    async def complete_structured_scoped(self, messages, **kwargs):
        self.ids.append(kwargs['request_id'])
        reply = self.replies[min(len(self.ids), len(self.replies)) - 1]
        return SimpleNamespace(text=reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False))


def run(store, writer, day=DAY):
    return asyncio.run(write_day(store, writer, day, ROWS, persona='p', now=NOW))


def test_a_claude_style_draft_with_long_labels_and_many_facts_is_saved(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    writer = Writer(CLAUDE_DRAFT, REVISION)
    assert run(store, writer) == 'written'
    saved = store.entry(DAY)
    assert writer.ids == ['diary:' + DAY, 'diary-revise:' + DAY]
    assert saved['title'] == '他的乐理考试，我比他还紧张一点点' and saved['mood'] == '心疼又好笑' and saved['body'] == BODY
    with store._db() as db:
        facts = json.loads(db.execute('SELECT facts FROM diary_entries').fetchone()[0])
    assert len(facts) == 8 and all(f['kind'] in diary.FACT_KINDS and len(f['text']) <= 60 for f in facts)
    assert [f['kind'] for f in facts[:3]] == ['user_event', 'anniversary', 'plan']
    assert facts[2]['date'] is None                       # the missing date key is a null date, not a lost fact
    assert 'event' not in {f['kind'] for f in facts} and '周末一起去看海' not in json.dumps(facts, ensure_ascii=False)


def test_the_unrevised_draft_is_clipped_at_a_clause_when_the_edit_is_unusable(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    run(store, Writer(CLAUDE_DRAFT, '抱歉，我无法完成这个请求。'))
    saved = store.entry(DAY)
    assert saved['title'] == '他说下周三要考乐理' and saved['mood'] == '有点委屈' and saved['body'] == BODY


@pytest.mark.parametrize('wrap', [
    lambda value: fenced(value),
    lambda value: fenced(value, before='', after=''),
    lambda value: '```\n' + json.dumps(value, ensure_ascii=False) + '\n```',
    lambda value: '这是日记 ' + json.dumps(value, ensure_ascii=False) + ' 完',
    lambda value: json.dumps({'entry': value}, ensure_ascii=False),
    lambda value: json.dumps([value], ensure_ascii=False),
])
def test_json_in_a_fence_with_words_around_it_or_wrapped_once_is_read(tmp_path, wrap):
    store = DiaryStore(tmp_path / 'diary.db')
    assert run(store, Writer(wrap(CLAUDE_DRAFT), wrap({'title': '改好了', 'mood': '暖', 'body': BODY + '再添一笔。'}))) == 'written'
    assert store.entry(DAY)['body'].endswith('再添一笔。')


def test_missing_labels_are_made_from_the_draft_and_the_body(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    run(store, Writer({'body': '你说下周三要考乐理，我听出你有点紧张。' + BODY}, {'body': BODY}))
    saved = store.entry(DAY)
    assert saved['title'] == '你说下周三要考乐理，我听出你有点紧张' and saved['mood'] == ''   # edit without labels keeps the draft's
    assert saved['body'] == BODY


def test_a_body_a_little_over_the_limit_is_kept_and_one_far_over_it_ends_at_a_sentence():
    limit = 1500
    sentence = '这是一整句话。'
    kept = validate({**ENTRY, 'body': sentence * 230}, [])['body']                  # 1,610 characters
    assert len(kept) == 230 * len(sentence)
    clipped = validate({**ENTRY, 'body': sentence * 600}, [])['body']              # 4,200 characters
    assert len(clipped) <= limit and clipped.endswith('。') and len(clipped) > limit // 2
    english = validate({**ENTRY, 'body': 'No full stop in sight ' * 400}, [])['body']
    assert len(english) <= limit


@pytest.mark.parametrize('reply,error', [
    ({'title': '有题目', 'mood': '平静', 'body': '', 'facts': []}, 'DIARY_ENTRY_EMPTY'),
    ({'title': '有题目', 'mood': '平静', 'facts': []}, 'DIARY_ENTRY_EMPTY'),
    ({'title': '有题目', 'mood': '平静', 'body': 12, 'facts': []}, 'DIARY_ENTRY_EMPTY'),
    ('我无法写这篇日记。', 'DIARY_NOT_JSON'),
    ('["一", "二"]', 'DIARY_ENTRY_INVALID'),
    ('{"title": "没写完', 'DIARY_NOT_JSON'),
])
def test_only_output_with_no_body_or_no_json_is_refused(tmp_path, reply, error):
    store = DiaryStore(tmp_path / 'diary.db')
    with pytest.raises(ValueError, match=error):
        run(store, Writer(reply))
    assert store.entry(DAY) is None


def test_english_labels_are_held_to_the_english_limits_not_the_chinese_ones():
    ui_language.set_language('en')
    entry = validate({**ENTRY, 'title': 'The exam you are nervous about next Wednesday', 'mood': 'tender and worried',
                      'body': 'You told me about the exam. ' * 120}, [])
    assert entry['title'] == 'The exam you are nervous about next Wednesday' and entry['mood'] == 'tender and worried'
    long = validate({**ENTRY, 'title': 'The exam you are nervous about, and how I kept thinking about it all night long and into the morning'}, [])
    assert long['title'] == 'The exam you are nervous about'                      # clipped at the clause, limit 80
    assert validate({**ENTRY, 'mood': 'tender, worried, and a little bit jealous of the exam'}, [])['mood'] == 'tender, worried'


@pytest.mark.parametrize('text,limit,expected', [
    ('有点委屈又有点想他', 8, '有点委屈'),
    ('嘴上嫌弃，心里偷偷软了一下', 8, '嘴上嫌弃'),
    ('特别特别特别特别特别想你呀', 8, '特别特别特别特别'),
    ('下周三的乐理考试，我好像比你还要紧张一点点', 20, '下周三的乐理考试'),
    ('心疼', 8, '心疼'),
    ('今天，他说了很多让我心里软软的话，还有那个约定', 20, '今天，他说了很多让我心里软软的话'),
    ('Tender and a little worried about tomorrow', 20, 'Tender and a little'),
])
def test_labels_are_clipped_at_a_natural_break(text, limit, expected):
    assert _clip(text, limit) == expected


# --- the gateway hands over what the model wrote ---------------------------------------------------------------------

def adapter(monkeypatch, *replies):
    gateway = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible', base_url='https://example.test/v1',
        model='claude-sonnet-5-5', api_style='chat_completions', provider_options={'capabilities': {}}))
    seen = []

    async def post(body, request, **kwargs):
        seen.append((request, body))
        text = replies[min(len(seen), len(replies)) - 1]
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': text}}]}

    monkeypatch.setattr(gateway, '_post_json', post)
    return gateway, seen


def test_a_reply_off_the_schema_reaches_the_diary_through_the_real_gateway(monkeypatch, tmp_path):
    gateway, seen = adapter(monkeypatch, fenced(CLAUDE_DRAFT), fenced(REVISION))
    store = DiaryStore(tmp_path / 'diary.db')
    assert run(store, gateway) == 'written'
    assert store.entry(DAY)['title'] == '他的乐理考试，我比他还紧张一点点' and len(seen) == 2
    # The writer is still told the full contract, lengths included; only the check on its answer moved to the diary.
    assert any('"maxLength": 8' in m['content'] for m in seen[0][1]['messages'])
    assert 'caller_validates' not in json.dumps(seen[0][1])


def test_the_gateway_still_checks_everyone_else(monkeypatch):
    gateway, seen = adapter(monkeypatch, json.dumps({**CLAUDE_DRAFT, 'facts': []}))
    plain = {'type': 'json_schema', 'json_schema': diary.FORMAT['json_schema']}
    with pytest.raises(ProviderProtocolError):
        asyncio.run(gateway.complete_structured_scoped([{'role': 'user', 'content': 'x'}], response_format=plain,
                                                       scope=GatewayRequestScope.BACKGROUND_REASONING))
    reply = asyncio.run(gateway.complete_structured_scoped([{'role': 'user', 'content': 'x'}], response_format=diary.FORMAT,
                                                           scope=GatewayRequestScope.BACKGROUND_REASONING))
    assert json.loads(reply.text)['mood'] == '有点委屈又有点想他'


@pytest.mark.parametrize('finish,text', [('length', '{"title": "没写完'), ('stop', '')])
def test_a_cut_off_or_empty_reply_is_still_an_error_at_the_gateway(monkeypatch, finish, text):
    gateway, _ = adapter(monkeypatch, text)

    async def post(body, request, **kwargs):
        return {'choices': [{'finish_reason': finish, 'message': {'content': text}}]}

    monkeypatch.setattr(gateway, '_post_json', post)
    with pytest.raises(ProviderProtocolError):
        asyncio.run(gateway.complete_structured_scoped([{'role': 'user', 'content': 'x'}], response_format=diary.FORMAT,
                                                       scope=GatewayRequestScope.BACKGROUND_REASONING))


# --- tries that never reached a service ------------------------------------------------------------------------------

def test_a_request_that_never_reached_a_service_keeps_its_attempt(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    for _ in range(5):
        store.deferred(DAY, 'ProviderUnavailable')
    with store._db() as db:
        assert db.execute('SELECT attempts FROM diary_attempts').fetchone()[0] == 0
    assert not store.may_attempt(DAY)                       # waits an hour
    with store._db() as db:
        db.execute('UPDATE diary_attempts SET last_at=0')
    assert store.may_attempt(DAY)
    store.failed(DAY, 'ProviderProtocolError')
    with store._db() as db:
        assert db.execute('SELECT attempts FROM diary_attempts').fetchone()[0] == 1


def test_which_failures_never_reached_a_service():
    assert unserved(ProviderUnavailable()) and unserved(ConnectionError()) and unserved(ConnectionResetError())
    assert unserved(GatewayError('PROVIDER_QUOTA_EXHAUSTED', retryable=False))
    assert not unserved(ProviderTimeout()) and not unserved(ProviderProtocolError('structured_validation_failed'))
    assert not unserved(ValueError('DIARY_ENTRY_EMPTY')) and not unserved(asyncio.TimeoutError())


def real_tick(monkeypatch, tmp_path, writer, store):
    import local_server as server
    from runtime.call import store as calls
    from runtime.private_world import daily_life_runtime
    for day in range(1, 7):
        store.skip(f'2026-10-0{day}', 'EMPTY_DAY')                # only the seventh is left to write
    monkeypatch.setattr(server, 'diary_store', store)
    monkeypatch.setattr(server, 'store', SimpleNamespace(letters=[row for row in ROWS], personal_chats=[]))
    monkeypatch.setattr(server, 'letters_adapter', SimpleNamespace(gateway=writer, persona_v2_path=None))
    monkeypatch.setattr(server, 'daily_life_runtime', None)
    monkeypatch.setattr(server, '_diary_extras', lambda day: ('unknown', [], None))
    monkeypatch.setattr(server, '_diary_visits', lambda day: [])
    monkeypatch.setattr(calls, 'diary_rows', lambda server: [])
    monkeypatch.setattr(daily_life_runtime, 'life_persona', lambda path: 'static')
    return server


class Failing:
    config = SimpleNamespace(model='claude-sonnet-5-5')

    def __init__(self, error):
        self.error, self.calls = error, 0

    async def complete_structured_scoped(self, messages, **kwargs):
        self.calls += 1
        raise self.error


def attempts(store, day=DAY):
    with store._db() as db:
        row = db.execute('SELECT attempts FROM diary_attempts WHERE day=?', (day,)).fetchone()
    return row[0] if row else None


def test_the_tick_keeps_attempts_when_offline_counts_other_failures_and_never_dies(monkeypatch, tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    server = real_tick(monkeypatch, tmp_path, Failing(ProviderUnavailable()), store)
    asyncio.run(server._diary_tick(NOW))
    assert attempts(store) == 0
    server.letters_adapter.gateway = Failing(ProviderProtocolError('structured_validation_failed'))
    with store._db() as db:
        db.execute('UPDATE diary_attempts SET last_at=0')
    asyncio.run(server._diary_tick(NOW))
    assert attempts(store) == 1
    server.letters_adapter.gateway = Failing(AttributeError('unexpected'))   # no failure may end the diary loop
    with store._db() as db:
        db.execute('UPDATE diary_attempts SET last_at=0')
    asyncio.run(server._diary_tick(NOW))
    assert attempts(store) == 2


def test_the_real_tick_writes_a_claude_style_day(monkeypatch, tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    server = real_tick(monkeypatch, tmp_path, Writer(CLAUDE_DRAFT, '没有json'), store)
    asyncio.run(server._diary_tick(NOW))
    assert store.entry(DAY)['title'] == '他说下周三要考乐理'


# --- days lost to refused replies get one more try -------------------------------------------------------------------

def use_up(store, day, reason):
    for _ in range(MAX_ATTEMPTS):
        store.failed(day, reason)


def lost_days(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    use_up(store, '2026-10-07', 'ProviderProtocolError')      # the draft came back and was refused
    use_up(store, '2026-10-05', 'JSONDecodeError')
    use_up(store, '2026-10-04', 'TimeoutError')               # the call ran out of the time it was given
    use_up(store, '2026-10-06', 'ProviderUnavailable')        # nothing was reachable: not this fault
    use_up(store, '2026-10-03', 'ProviderTimeout')            # the service itself stalled
    use_up(store, '2026-10-02', 'ProviderProtocolError')
    store.save('2026-10-02', {**ENTRY, 'facts': []}, now=NOW, model='m', short=False)   # written some other way since
    use_up(store, '2026-10-01', 'ProviderProtocolError')
    store.delete('2026-10-01')                                # the user removed it
    use_up(store, '2026-09-20', 'ProviderProtocolError')      # older than the catch-up window
    return store


def test_days_lost_to_refused_replies_are_tried_once_more_and_only_once(tmp_path):
    store = lost_days(tmp_path)
    assert due_days(store, NOW) == ['2026-10-04', '2026-10-05', '2026-10-07']
    assert attempts(store, '2026-10-07') == MAX_ATTEMPTS - 1
    assert due_days(store, NOW) == ['2026-10-04', '2026-10-05', '2026-10-07']   # asking again changes nothing
    store.failed('2026-10-07', 'ProviderProtocolError')           # the one extra try is refused as well
    assert due_days(store, NOW) == ['2026-10-04', '2026-10-05']
    assert due_days(DiaryStore(store.path), NOW) == ['2026-10-04', '2026-10-05']  # a restart does not reopen it
    assert attempts(store, '2026-10-07') == MAX_ATTEMPTS
    assert attempts(store, '2026-09-20') == MAX_ATTEMPTS and attempts(store, '2026-10-06') == MAX_ATTEMPTS


def test_a_later_reopen_key_gives_lost_days_two_more_tries_once(tmp_path):
    # An install that used the first reopen earlier, then lost days again while a supplier dropped the system text.
    store = DiaryStore(tmp_path / 'diary.db')
    with store._db() as db:
        db.execute('INSERT INTO diary_settings VALUES (?,?)', (diary.RETRY_ONCE_KEY, '1'))
    use_up(store, '2026-10-07', 'ValueError')                 # the reply had no diary in it
    use_up(store, '2026-10-06', 'ProviderUnavailable')        # nothing was reachable: not this fault
    due = due_days(store, NOW)
    assert '2026-10-07' in due and '2026-10-06' not in due
    assert attempts(store, '2026-10-07') == MAX_ATTEMPTS - 2
    store.failed('2026-10-07', 'ValueError')
    with store._db() as db:
        db.execute('UPDATE diary_attempts SET last_at=0 WHERE day=?', ('2026-10-07',))
    assert '2026-10-07' in due_days(DiaryStore(store.path), NOW)     # the second try is still there after a restart
    store.failed('2026-10-07', 'ValueError')
    assert '2026-10-07' not in due_days(DiaryStore(store.path), NOW)  # and the key never reopens it again


def test_empty_days_stay_closed(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    store.skip('2026-10-07', 'EMPTY_DAY')
    assert DAY not in due_days(store, NOW)


def test_the_extra_try_writes_the_day_without_the_user_doing_anything(tmp_path):
    store = lost_days(tmp_path)
    writer = Writer(CLAUDE_DRAFT, REVISION)
    assert DAY in due_days(store, NOW)
    assert run(store, writer) == 'written'
    assert store.entry(DAY)['mood'] == '心疼又好笑'
    assert writer.ids == ['diary:' + DAY, 'diary-revise:' + DAY]
    assert DAY not in due_days(store, NOW)


def test_the_extra_try_through_the_real_tick(monkeypatch, tmp_path):
    store = lost_days(tmp_path)
    server = real_tick(monkeypatch, tmp_path, Writer(CLAUDE_DRAFT, REVISION), store)
    asyncio.run(server._diary_tick(NOW))
    assert store.entry(DAY)['title'] == '他的乐理考试，我比他还紧张一点点' and store.entry(DAY)['mood'] == '心疼又好笑'


def test_a_new_install_has_nothing_to_reopen_and_later_failures_are_final(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    assert store.retry_rejected([DAY]) == []
    use_up(store, DAY, 'ProviderProtocolError')
    assert DAY not in due_days(store, NOW)
