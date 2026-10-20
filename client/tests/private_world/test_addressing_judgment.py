import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from runtime.model_routes import routed_model
from runtime.private_world import addressing_judgment as judgment
from runtime.private_world.daily_life import DailyLifeStore

CST = timezone(timedelta(hours=8))
DAY = datetime(2026, 10, 20, 9, 0, tzinfo=CST)
NEXT_DAY = DAY + timedelta(days=1)
BASE_URL = 'https://relay.example/v1'


def letter(index, user, reply, at=DAY, **extra):
    stamp = (at - timedelta(hours=1) + timedelta(minutes=index)).isoformat()
    return {'letter_id': f'l{index}', 'channel': 'letter', 'letter_status': 'COMPLETED', 'content': user,
            'reply_text': reply, 'replied_at': stamp, **extra}


ROWS = [letter(0, '小离今天想吃什么', '还没想好呢。'),
        letter(1, '我下班啦', '早安呀饭饭，今天也辛苦了。'),
        letter(2, '好吧', '那你先去吃饭。')]


class FakeGateway:
    """Answers with a fixed JSON reply; records each request and the model it was pinned to."""

    def __init__(self, answer=None, *, fail=None, delay=0.0):
        self.answer, self.fail, self.delay = answer or {}, fail, delay
        self.config = SimpleNamespace(base_url=BASE_URL)
        self.requests = []

    async def complete_structured_scoped(self, messages, *, response_format, scope, request_id=None):
        self.requests.append({'messages': messages, 'format': response_format, 'request_id': request_id,
                              'model': routed_model(BASE_URL, 'main-model')})
        if self.fail:
            raise self.fail
        if self.delay:
            await asyncio.sleep(self.delay)
        answer = {field: self.answer.get(field) for field in judgment.FIELDS}
        return SimpleNamespace(text='```json\n' + json.dumps(answer, ensure_ascii=False) + '\n```')


def run(store, gateway, rows=ROWS, now=DAY, **kwargs):
    return asyncio.run(judgment.run_daily(store, rows, gateway, now, **kwargs))


def shown(store, kind, now=NEXT_DAY + timedelta(days=5)):
    return [item['quote'] for item in store.addressing_profile(now=now).get(kind, [])]


def test_the_terms_named_in_the_right_side_of_the_named_exchange_are_shown(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    gateway = FakeGateway({'she_calls_him': {'term': '饭饭', 'id': 'e1'}, 'he_calls_her': {'term': '小离', 'id': 'e0'}})
    assert run(store, gateway) == 'judged'
    assert shown(store, 'linli_calls_user') == ['饭饭'] and shown(store, 'user_calls_linli') == ['小离']
    assert 'user_self' not in store.addressing_profile(now=NEXT_DAY)
    # The time shown is when the term was last used, not when it was judged.
    stamp = store.addressing_profile(now=NEXT_DAY)['linli_calls_user'][0]['occurred_at']
    assert datetime.fromisoformat(stamp) == datetime.fromisoformat(ROWS[1]['replied_at'])


def test_a_term_that_side_did_not_write_there_is_dropped(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    gateway = FakeGateway({'he_calls_her': {'term': '饭饭', 'id': 'e1'},       # her words, not his
                           'she_calls_him': {'term': '饭饭', 'id': 'e0'},      # not in that exchange
                           'he_self': {'term': '你', 'id': 'e0'}})              # not a name
    assert run(store, gateway) == 'judged'
    assert store.addressing_profile(now=NEXT_DAY) == {}


def test_after_the_first_judgment_the_old_quotes_no_longer_show(tmp_path):
    # 2026-10-20: the per-message quotes left words such as 好吧 as what the user calls her.
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    store.record_addressing('reply:a:1', {'user_calls_linli': '好吧'}, occurred_at=DAY - timedelta(hours=2))
    assert shown(store, 'user_calls_linli') == ['好吧']  # until the daily judgment has run once
    assert run(store, FakeGateway({})) == 'judged'
    assert shown(store, 'user_calls_linli') == []


def test_a_kind_the_judgment_does_not_name_keeps_its_term_and_a_new_term_comes_first(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    assert run(store, FakeGateway({'he_calls_her': {'term': '小离', 'id': 'e0'}})) == 'judged'
    rows = [*ROWS, letter(3, '在吗', '在呢。', at=NEXT_DAY)]
    gateway = FakeGateway({})
    assert run(store, gateway, rows, NEXT_DAY) == 'judged'
    assert shown(store, 'user_calls_linli') == ['小离']
    # The judgment is told the term it named before.
    packet = json.loads(gateway.requests[0]['messages'][1]['content'])
    assert packet['previous'] == {'she_calls_him': None, 'he_calls_her': '小离', 'he_self': None}
    rows = [*rows, letter(4, '离离晚安', '晚安。', at=NEXT_DAY + timedelta(days=1))]
    assert run(store, FakeGateway({'he_calls_her': {'term': '离离', 'id': 'e4'}}), rows, NEXT_DAY + timedelta(days=1)) == 'judged'
    assert shown(store, 'user_calls_linli') == ['离离', '小离']


def test_once_a_day_and_only_when_there_is_something_new(tmp_path):
    store, gateway = DailyLifeStore(tmp_path / 'life.sqlite3'), FakeGateway({})
    assert run(store, gateway) == 'judged'
    assert run(store, gateway, now=DAY + timedelta(hours=5)) == 'done_today'
    assert run(store, gateway, now=NEXT_DAY) == 'idle'  # nothing new since
    assert run(store, gateway, [*ROWS, letter(3, '在吗', '在呢。', at=NEXT_DAY)], NEXT_DAY) == 'judged'
    assert len(gateway.requests) == 2
    assert run(DailyLifeStore(tmp_path / 'empty.sqlite3'), gateway, []) == 'idle'


def test_a_failed_request_is_tried_again_an_hour_later(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    store.record_addressing('reply:a:1', {'user_calls_linli': '小离'}, occurred_at=DAY - timedelta(hours=2))
    assert run(store, FakeGateway(fail=OSError('offline'))) == 'unavailable'
    assert shown(store, 'user_calls_linli') == ['小离']  # what showed before stays
    assert run(store, FakeGateway({}), now=DAY + timedelta(minutes=30)) == 'waiting'
    assert run(store, FakeGateway({}), now=DAY + timedelta(minutes=61)) == 'judged'


def test_a_slow_request_is_given_up(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    assert run(store, FakeGateway({}, delay=1.0), timeout=0.05) == 'unavailable'


def test_the_request_reads_the_newest_exchanges_on_the_pinned_model(tmp_path):
    store, gateway = DailyLifeStore(tmp_path / 'life.sqlite3'), FakeGateway({})
    long = '小离' + '很长的一段话' * 100 + '晚安离离'
    rows = [letter(i, f'第{i}条', f'回{i}') for i in range(judgment.WINDOW + 5)]
    rows += [letter(99, long, '好。'), {**letter(98, '没送到', '没送到'), 'letter_status': 'FAILED'},
             {**letter(97, '只读', '只读'), 'read_only': True}]
    assert run(store, gateway, rows) == 'judged'
    request = gateway.requests[0]
    assert request['model'] == judgment.MODEL and request['request_id'] == 'addressing:2026-10-20'
    assert request['format']['json_schema']['name'] == 'addressing_terms'
    packet = json.loads(request['messages'][1]['content'])
    texts = [item['user'] for item in packet['exchanges']]
    assert len(texts) == judgment.WINDOW and '没送到' not in texts and '只读' not in texts
    assert texts[0] == '第6条'  # the oldest ones beyond the window are left out
    clipped = texts[-1]
    assert clipped.startswith('小离') and clipped.endswith('晚安离离') and '…' in clipped
    assert len(clipped) == judgment.HEAD + judgment.TAIL + 1
