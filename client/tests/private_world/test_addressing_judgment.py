import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from runtime.private_world import addressing_judgment as judgment
from runtime.private_world.addressing import address_term, needs_judgment, normalize, quote_key
from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime

CST = timezone(timedelta(hours=8))
DAY = datetime(2026, 10, 19, 2, 0, tzinfo=CST)
NEXT_DAY = DAY + timedelta(days=1)

# Synthetic quotes and the term a correct judgment picks from each (None: the quote holds no term of address).
CASES = [
    ('linli_calls_user', '早安呀饭饭', '饭饭'),
    ('linli_calls_user', '外边下雨了小猫咪', '小猫咪'),
    ('user_calls_linli', '小离今天想吃什么', '小离'),
    ('user_calls_linli', '宝贝你在干嘛呀', '宝贝'),
    ('linli_calls_user', '老姜你早点睡吧', '老姜'),
    ('user_calls_linli', '今天降温了离离', '离离'),
    ('linli_calls_user', '好呀老姜，', '老姜'),
    ('user_calls_linli', '晚安好梦做个好梦', None),
    ('linli_calls_user', '今天天气真好呀', None),
    ('user_calls_linli', '你吃饭了吗今天', None),
    ('linli_calls_user', '饭饭，你终于回来啦', '饭饭'),
    ('user_calls_linli', '亲爱的小离', '小离'),
]


class FakePort:
    """Answers each item with the span a correct judgment picks; counts requests."""

    def __init__(self, expected=None, *, fail=None, delay=0.0):
        self.expected = {normalize(quote): term for _kind, quote, term in CASES} if expected is None else expected
        self.fail, self.delay = fail, delay
        self.requests = []

    async def ask(self, state, questions, *, purpose):
        self.requests.append((state, questions, purpose))
        if self.fail:
            raise self.fail
        if self.delay:
            await asyncio.sleep(self.delay)
        answers = {}
        for item in state['items']:
            term = self.expected.get(item['message'])
            answers[item['id']] = 'none' if term is None else term
        return answers


def make_store(tmp_path):
    return DailyLifeStore(tmp_path / 'life.sqlite3')


def record_all(store, cases=CASES, *, at=DAY):
    for index, (kind, quote, _term) in enumerate(cases):
        store.record_addressing(f'reply:c{index}:1', {kind: quote}, occurred_at=at + timedelta(seconds=index))


def batch(store, port, now=DAY, **kwargs):
    return asyncio.run(judgment.run_daily_batch(store, now, port_factory=lambda: port, **kwargs))


def terms(store, kind, now=NEXT_DAY + timedelta(days=1)):
    return {item['quote'] for item in store.addressing_profile(now=now, per_kind=50).get(kind, [])}


def test_short_quotes_are_judged_too_and_show_the_shape_until_then(tmp_path):
    # 2026-10-19: a reply of two characters (好吧) passed the shape check and showed as what the user calls her.
    store = make_store(tmp_path)
    store.record_addressing('reply:a:1', {'user_calls_linli': '小离，', 'linli_calls_user': '饭饭呀'}, occurred_at=DAY)
    store.record_addressing('reply:a:2', {'user_calls_linli': '好吧'}, occurred_at=DAY + timedelta(seconds=1))
    assert len(store.pending_addressing(20)) == 3
    assert terms(store, 'user_calls_linli') == {'小离', '好吧'}
    assert batch(store, FakePort({'小离': '小离', '饭饭呀': '饭饭', '好吧': None})) == 'judged'
    assert terms(store, 'user_calls_linli') == {'小离'} and terms(store, 'linli_calls_user') == {'饭饭'}
    assert needs_judgment('user_calls_linli', '亲爱的') and needs_judgment('user_calls_linli', '早安呀饭饭')


def test_the_judgment_reads_the_whole_message_the_quote_came_from(tmp_path):
    # 2026-10-19: "我说我打棒球是棒球手" left 棒球手 as what the user calls her; the quote alone cannot tell.
    store = make_store(tmp_path)
    store.record_addressing('reply:b:1', {'user_calls_linli': '棒球手'}, occurred_at=DAY)
    with store._db() as db:
        db.execute('INSERT INTO life_exchange_user_text VALUES (?,?)', ('reply:b:1', '我打棒球，是棒球手，哈哈哈'))
    port = FakePort({'我打棒球，是棒球手，哈哈哈': None})
    assert batch(store, port) == 'judged'
    item = port.requests[0][0]['items'][0]
    assert item['message'] == '我打棒球，是棒球手，哈哈哈' and item['quote'] == '棒球手'
    assert terms(store, 'user_calls_linli') == set()


def test_the_users_own_name_is_never_judged(tmp_path):
    store = make_store(tmp_path)
    store.record_addressing('reply:a:1', {'user_self': '你的老姜老姜老姜'}, occurred_at=DAY)
    assert store.pending_addressing(20) == []
    assert not needs_judgment('user_self', '你的老姜老姜老姜')


def shape_terms(kind, cases=CASES):
    return {address_term(quote) for k, quote, _term in cases if k == kind} - {None}


def test_longer_quotes_show_the_shape_check_until_they_are_judged(tmp_path):
    store = make_store(tmp_path)
    record_all(store)
    assert len(store.pending_addressing(20)) == len(CASES)
    # Nothing is asked yet: the profile is what the shape check makes of each quote, which may be nothing.
    for kind in ('linli_calls_user', 'user_calls_linli'):
        assert terms(store, kind) == shape_terms(kind)
    assert {'饭饭', '小猫咪'} <= terms(store, 'linli_calls_user')


def test_one_request_judges_every_waiting_quote(tmp_path):
    store, port = make_store(tmp_path), FakePort()
    record_all(store)
    assert batch(store, port) == 'judged'
    assert len(port.requests) == 1
    state, questions, purpose = port.requests[0]
    assert purpose == 'addressing-term'
    assert [item['id'] for item in state['items']] == [f'q{i}' for i in range(len(CASES))]
    assert {item['speaker'] for item in state['items']} == {'user', 'her'}
    assert set(questions) == {item['id'] for item in state['items']}
    # The request is slim: a one-line label per span, no objects, and the question names its item.
    for key, question in questions.items():
        assert question['instructions'].startswith(f'针对 items 里 id 为 {key} 的那一条：')
        assert question['instructions'].endswith(judgment.INSTRUCTIONS)
        assert question['criteria']['none'] == '没有称呼对方'
        assert all(isinstance(text, str) for text in question['criteria'].values())
        assert all(text == f'称呼「{span}」' for span, text in question['criteria'].items() if span != 'none')
    assert terms(store, 'linli_calls_user') == {'饭饭', '小猫咪', '老姜'}
    assert terms(store, 'user_calls_linli') == {'小离', '宝贝', '离离'}
    assert store.pending_addressing(20) == []


def test_candidate_spans_cover_short_clauses_and_the_edges_of_long_ones():
    spans = judgment.candidate_spans('外边下雨了小猫咪')
    assert {'外', '边下', '小猫咪', '下雨了小猫咪', '外边下雨了小猫咪'} <= set(spans)  # a short clause offers every span
    assert '外边下雨了小猫' not in spans  # longer than six and not the whole clause
    long = '早安呀饭饭好久不见的你今天过得怎么样'
    assert '好久不见' not in judgment.candidate_spans(long) and '早安呀饭饭' in judgment.candidate_spans(long)
    assert len(judgment.candidate_spans(long)) == len(set(judgment.candidate_spans(long)))
    # Only word characters can be a term, and each clause has its own spans.
    assert set(judgment.candidate_spans('早安，饭饭')) == {'早', '安', '早安', '饭', '饭饭'}
    # A name inside a clause stands on its own (2026-10-19: 那头号粉丝要一直支持我哦, 好呀老姜).
    assert '头号粉丝' in judgment.candidate_spans('那头号粉丝要一直支持我哦')
    assert '老姜' in judgment.candidate_spans('好呀老姜，那我以后就这么叫你')


def test_a_judged_quote_is_never_judged_again(tmp_path):
    store, port = make_store(tmp_path), FakePort()
    record_all(store)
    assert batch(store, port) == 'judged'
    # Same words again (also with other closing punctuation): stored as the judged term, nothing waits.
    store.record_addressing('reply:again:1', {'linli_calls_user': '早安呀饭饭！'}, occurred_at=NEXT_DAY)
    store.record_addressing('reply:again:2', {'user_calls_linli': '晚安好梦做个好梦'}, occurred_at=NEXT_DAY)
    assert store.pending_addressing(20) == []
    assert batch(store, port, NEXT_DAY) == 'idle'
    assert len(port.requests) == 1
    with store._db() as db:
        assert db.execute("SELECT quote FROM life_addressing WHERE source_id='reply:again:1'").fetchone()[0] == '饭饭'
        assert db.execute("SELECT count(*) FROM life_addressing WHERE source_id='reply:again:2'").fetchone()[0] == 0


def test_the_same_words_of_the_other_kind_are_judged_separately(tmp_path):
    assert quote_key('linli_calls_user', '早安呀饭饭') != quote_key('user_calls_linli', '早安呀饭饭')
    assert quote_key('linli_calls_user', normalize('早安呀饭饭，')) == quote_key('linli_calls_user', '早安呀饭饭')


def test_at_most_one_request_per_profile_and_day(tmp_path):
    store, port = make_store(tmp_path), FakePort()
    record_all(store, CASES[:3])
    assert batch(store, port) == 'judged'
    record_all(store, [('linli_calls_user', '饭饭，你终于回来啦', '饭饭')], at=DAY + timedelta(hours=3))
    assert batch(store, port, DAY + timedelta(hours=5)) == 'done_today'
    assert len(port.requests) == 1
    assert batch(store, port, NEXT_DAY) == 'judged'
    assert len(port.requests) == 2 and len(port.requests[1][0]['items']) == 1


def test_a_failed_request_keeps_the_shape_check_and_counts_for_the_day(tmp_path):
    store = make_store(tmp_path)
    record_all(store)
    before = terms(store, 'linli_calls_user')
    failing = FakePort(fail=RuntimeError('JEV_UNAVAILABLE'))
    assert batch(store, failing) == 'unavailable'
    assert terms(store, 'linli_calls_user') == before
    assert len(store.pending_addressing(20)) == len(CASES)
    assert batch(store, FakePort(), DAY + timedelta(hours=1)) == 'done_today'
    assert batch(store, FakePort(), NEXT_DAY) == 'judged'  # tried again the next day, nothing was lost
    assert terms(store, 'linli_calls_user') == {'饭饭', '小猫咪', '老姜'}


def test_a_slow_request_is_given_up(tmp_path):
    store = make_store(tmp_path)
    record_all(store, CASES[:2])
    assert batch(store, FakePort(delay=2), timeout=0.05) == 'unavailable'
    assert len(store.pending_addressing(20)) == 2


@pytest.mark.parametrize('reply', [
    {'q0': '不在选项里'},                 # not one of the offered spans
    {'q0': '饭饭', 'q1': 'none'},         # an answer for a question that was not asked
    {},                                    # an answer missing
    'none',                                # not even a mapping
])
def test_an_invalid_answer_changes_nothing(tmp_path, reply):
    class Port:
        async def ask(self, state, questions, *, purpose):
            return reply

    store = make_store(tmp_path)
    record_all(store, CASES[:1])
    assert batch(store, Port()) == 'unavailable'
    assert len(store.pending_addressing(20)) == 1
    with store._db() as db:
        assert db.execute("SELECT count(*) FROM life_addressing_judgments WHERE status='judged'").fetchone()[0] == 0


def test_a_chosen_span_must_still_have_the_shape_of_a_term(tmp_path):
    store = make_store(tmp_path)
    store.record_addressing('reply:a:1', {'linli_calls_user': '好的我知道了'}, occurred_at=DAY)
    assert batch(store, FakePort({'好的我知道了': '好的'})) == 'judged'
    assert terms(store, 'linli_calls_user') == set()


def test_a_request_carries_at_most_twenty_questions(tmp_path):
    store, port = make_store(tmp_path), FakePort(expected={})
    for index in range(25):
        store.record_addressing(f'reply:n{index}:1', {'linli_calls_user': f'今天第{index}次说好久不见'},
                                occurred_at=DAY + timedelta(seconds=index))
    assert batch(store, port) == 'judged'
    assert len(port.requests[0][1]) == 20
    # The newest wait for no one: the five oldest are asked the next day.
    assert {item['message'] for item in port.requests[0][0]['items']} == {f'今天第{i}次说好久不见' for i in range(5, 25)}
    assert len(store.pending_addressing(30)) == 5
    assert batch(store, port, NEXT_DAY) == 'judged' and len(port.requests[1][1]) == 5


def insert_legacy(store, rows):
    """Rows as stored before the judgment: the quote as written."""
    with store._db() as db:
        for source_id, kind, quote, at in rows:
            db.execute('INSERT INTO life_addressing VALUES (?,?,?,?)', (source_id, kind, quote, at.astimezone(timezone.utc).isoformat()))


def test_stored_quotes_from_before_are_judged_in_the_same_daily_request(tmp_path):
    store = make_store(tmp_path)
    insert_legacy(store, [(f'reply:old{i}:1', kind, quote, DAY - timedelta(days=2, seconds=-i))
                          for i, (kind, quote, _term) in enumerate(CASES)])
    insert_legacy(store, [('reply:oldshort:1', 'user_calls_linli', '小离', DAY - timedelta(days=3))])
    # The profile of an old install is the shape check until the daily request.
    assert terms(store, 'linli_calls_user') == shape_terms('linli_calls_user')
    port = FakePort({**{normalize(quote): term for _kind, quote, term in CASES}, '小离': '小离'})
    assert batch(store, port) == 'judged'
    # Short stored quotes are judged as well since 2.4.10.
    assert len(port.requests) == 1 and len(port.requests[0][0]['items']) == len(CASES) + 1
    assert terms(store, 'linli_calls_user') == {'饭饭', '小猫咪', '老姜'}
    assert terms(store, 'user_calls_linli') == {'小离', '宝贝', '离离'}
    assert store.setting(judgment.LEGACY_KEY) == '1'
    # Once: the next day queues nothing from the old rows again, and nothing judged is asked twice.
    assert batch(store, port, NEXT_DAY) == 'idle' and len(port.requests) == 1


def test_the_cleanup_of_stored_quotes_is_capped_removes_none_and_runs_once(tmp_path):
    names = '甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥天地玄'
    quote = lambda i: f'外边下雨了小猫{names[i]}'
    store = make_store(tmp_path)
    # 25 distinct quotes, three of them repeated later; each shows a term by shape until it is judged.
    insert_legacy(store, [(f'reply:old{i}:1', 'linli_calls_user', quote(i % 25), DAY - timedelta(days=5, seconds=-i))
                          for i in range(28)])
    assert len(terms(store, 'linli_calls_user')) == 25
    port = FakePort(expected={})  # the judgment finds no term of address in any of them
    assert batch(store, port) == 'judged'
    asked = [item['message'] for item in port.requests[0][0]['items']]
    assert len(asked) == len(set(asked)) == 20
    # The twenty newest (the repeated ones count by their latest use) were asked and are gone from the profile;
    # the other five keep their shape term.
    assert terms(store, 'linli_calls_user') == {f'小猫{names[i]}' for i in range(3, 8)}
    assert not {quote(i) for i in range(3, 8)} & set(asked)
    assert batch(store, port, NEXT_DAY) == 'idle' and len(port.requests) == 1


class TickEnv:
    """A relationship batch tick with its own network stubbed, and a semantic port that records its requests."""

    def __init__(self, monkeypatch, tmp_path, *, due=True, nickname_fails=False, batch_fails=False):
        from types import SimpleNamespace
        from runtime import relationship_batch, relationship_growth, relationship_lite, relationship_moments
        self.events = []
        self.life = make_store(tmp_path)
        self.batch_fails, self.nickname_fails = batch_fails, nickname_fails
        events = self.events
        queue = SimpleNamespace(
            sync=lambda *args: None, due=lambda now: due, pending=lambda now: {'request_id': 'synthetic', 'turns': []},
            result=lambda identity: None, save_result=lambda *args: None,
            finish=lambda *args: events.append('commit'), defer=lambda now: events.append('defer'))
        monkeypatch.setattr(relationship_batch, 'BatchQueue', lambda path: queue)
        monkeypatch.setattr(relationship_lite, 'ledger', lambda server: SimpleNamespace(_database_path='unused', snapshot=lambda: None))
        monkeypatch.setattr(relationship_growth, 'route', lambda snapshot: 'friend')
        monkeypatch.setattr(relationship_moments, 'sync_messages', lambda *args: None)
        env = self

        class Port:
            async def ask(self, state, questions, *, purpose):
                events.append('nickname-request')
                if env.nickname_fails:
                    raise RuntimeError('JEV_UNAVAILABLE')
                return await FakePort().ask(state, questions, purpose=purpose)

        monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: Port())
        self.server = SimpleNamespace(
            store=SimpleNamespace(letters=[], personal_chats=[]), letters_adapter=SimpleNamespace(gateway=None),
            daily_life_runtime=SimpleNamespace(store=self.life), _refresh_contact_relationship_projection=lambda: None,
            _safe_log=lambda event, **fields: events.append(event))
        self.tick = relationship_batch.tick

    async def request(self, gateway, batch, route):
        self.events.append('relationship-request')
        if self.batch_fails:
            raise RuntimeError('private text')
        return {'turns': []}

    def run(self, now=DAY):
        return asyncio.run(self.tick(self.server, now, self.request))


def test_nicknames_are_judged_in_the_relationship_batch_tick_right_after_it(tmp_path, monkeypatch):
    env = TickEnv(monkeypatch, tmp_path)
    record_all(env.life)
    assert env.run() == 'updated'
    assert env.events == ['relationship-request', 'commit', 'nickname-request']
    assert terms(env.life, 'linli_calls_user') == {'饭饭', '小猫咪', '老姜'}


def test_no_nickname_request_while_no_relationship_batch_is_due(tmp_path, monkeypatch):
    env = TickEnv(monkeypatch, tmp_path, due=False)
    record_all(env.life)
    assert env.run() == 'waiting' and env.events == []
    assert len(env.life.pending_addressing(20)) == len(CASES)


def test_a_failed_nickname_request_leaves_the_relationship_batch_alone(tmp_path, monkeypatch):
    env = TickEnv(monkeypatch, tmp_path, nickname_fails=True)
    record_all(env.life)
    assert env.run() == 'updated'
    assert env.events == ['relationship-request', 'commit', 'nickname-request', 'addressing_judgment_deferred']
    assert terms(env.life, 'linli_calls_user') == shape_terms('linli_calls_user')


def test_a_failed_relationship_batch_does_not_stop_the_nickname_request(tmp_path, monkeypatch):
    env = TickEnv(monkeypatch, tmp_path, batch_fails=True)
    record_all(env.life)
    assert env.run() == 'deferred'
    assert env.events[:3] == ['relationship-request', 'defer', 'relationship_batch_deferred']
    assert env.events[3:] == ['nickname-request']
    assert terms(env.life, 'linli_calls_user') == {'饭饭', '小猫咪', '老姜'}


def test_ticks_send_the_nickname_request_once_a_day(tmp_path, monkeypatch):
    env = TickEnv(monkeypatch, tmp_path)
    record_all(env.life, CASES[:3])
    env.run()
    record_all(env.life, [('linli_calls_user', '饭饭，你终于回来啦', '饭饭')], at=DAY + timedelta(hours=1))
    env.run(DAY + timedelta(hours=6))  # the relationship batch is due again; the nickname request is not
    assert env.events.count('nickname-request') == 1
    env.run(NEXT_DAY)
    assert env.events.count('nickname-request') == 2


def test_a_client_without_a_life_store_ticks_as_before(tmp_path, monkeypatch):
    env = TickEnv(monkeypatch, tmp_path)
    env.server.daily_life_runtime = None
    assert env.run() == 'updated' and env.events == ['relationship-request', 'commit']


def test_exchange_commit_neither_waits_for_nor_needs_the_judgment(tmp_path, monkeypatch):
    purposes = []

    class Port:
        async def ask(self, state, questions, *, purpose):
            purposes.append(purpose)
            if purpose == 'addressing-term':
                raise RuntimeError('JEV_UNAVAILABLE')
            answers = {k: 'none' if 'none' in q['criteria'] else next(iter(q['criteria'])) for k, q in questions.items()}
            if purpose == 'exchange-facts':
                quote = next(k for k, (s, e) in state['quotes'].items() if k.startswith('r')
                             and state['sources']['linli_reply'][s:e] == '早安呀饭饭，')
                answers.update(address_user_quote=quote, world_update='none')
            return answers

    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: Port())
    store = make_store(tmp_path)
    runtime = DailyLifeRuntime(store, lambda: None, lambda: '')
    now = datetime(2026, 10, 19, 4, tzinfo=timezone.utc)
    assert asyncio.run(runtime.consume_exchange('reply:x:1', '今天项目返工了。', '早安呀饭饭，辛苦了。', occurred_at=now))
    assert 'addressing-term' not in purposes
    assert store.has_source('reply:x:1')
    assert [q['quote'] for q in store.addressing_profile(now=now)['linli_calls_user']] == ['饭饭']
    assert [row[2] for row in store.pending_addressing(20)] == ['早安呀饭饭']
    # The daily request fails: the exchange stays committed and the name stays what the shape check made of it.
    assert asyncio.run(judgment.run_daily_batch(store, now + timedelta(days=1), port_factory=Port)) == 'unavailable'
    assert [q['quote'] for q in store.addressing_profile(now=now + timedelta(days=1))['linli_calls_user']] == ['饭饭']


def test_receipt_verification_accepts_the_addressing_purpose():
    from runtime.diagnostics.jev_pricing import CURRENT_PRICE_VERSION, INPUT_RATE_UNITS, settlement_rates
    # The purpose is not in the table of listed purposes: the version's own rate verifies, so receipts are unchanged.
    assert INPUT_RATE_UNITS[CURRENT_PRICE_VERSION] in settlement_rates(CURRENT_PRICE_VERSION, judgment.PURPOSE)


def test_the_request_timeout_bounds_the_transport_too():
    class Transport:
        timeout_seconds = 50

    class Port:
        transport = Transport()

    judgment._bounded(Port, 25)
    assert Port.transport.timeout_seconds == 24


def test_the_batch_works_over_the_real_semantic_transport(tmp_path, monkeypatch):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from runtime.reply.jev_semantic_service import decide
    expected = {normalize(quote): term for _kind, quote, term in CASES}
    seen = []

    class Native:
        def ask(self, state, questions):
            seen.append((self.purpose, state, questions))
            messages = {item['id']: item['message'] for item in state['items']}
            return {key: {'type': 'choice', 'choice': expected[messages[key]] or 'none'} for key in questions}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            assert self.path == '/v1/companion/semantic-decisions'
            packet = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            body = json.dumps(dict(decide(Native(), packet), backend='jev')).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv('OLIVIA_JEV_DECISION_URL', f'http://127.0.0.1:{server.server_port}/v1/companion/decide')
        monkeypatch.setenv('COMPANION_CLASSIFIER_TOKEN', 'synthetic')
        store = make_store(tmp_path)
        record_all(store)
        assert asyncio.run(judgment.run_daily_batch(store, DAY)) == 'judged'
    finally:
        server.shutdown()
        server.server_close()
    assert [purpose for purpose, _state, _questions in seen] == ['addressing-term']
    assert terms(store, 'linli_calls_user') == {'饭饭', '小猫咪', '老姜'}
    assert terms(store, 'user_calls_linli') == {'小离', '宝贝', '离离'}
