import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from runtime.call import context as call_context, desk, http as call_http, life as call_life, store as call_store
from tests.call.test_call_client import Request, Server, answer


class AppRequest(Request):
    app = {}


class FakeWS:
    def __init__(self):
        self.sent, self.closed = [], False

    async def send_json(self, value):
        self.sent.append(value)


def account(monkeypatch):
    monkeypatch.setenv('OLIVIA_GPU_API_URL', 'https://api.example.cn')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'secret')


def test_config_says_whether_the_service_takes_phone_calls(monkeypatch):
    account(monkeypatch)

    async def cloud(method, path, *, timeout, headers=None, body=None):
        return 200, {'available': True, 'cents_per_minute': 40, 'phone': True, 'models': []}
    monkeypatch.setattr(call_http, '_cloud_json', cloud)
    status, value = answer(asyncio.run(call_http.call_http(Server(), Request('/toy/call/config'))))
    assert status == 200 and value['data']['phone'] is True


def test_pairing_link_needs_confirmation_and_keeps_the_key_here(monkeypatch):
    account(monkeypatch)
    sent = {}

    async def cloud(method, path, *, timeout, headers=None, body=None):
        sent.update(method=method, path=path, headers=headers)
        return 200, {'code': 'c' * 24, 'expires_in': 600, 'url': 'https://bside-moon.cn/call/#pair=' + 'c' * 24}
    monkeypatch.setattr(call_http, '_cloud_json', cloud)
    status, _ = answer(asyncio.run(call_http.call_http(Server(), Request('/toy/call/phone/pair', 'POST', confirmed=False))))
    assert status == 403
    status, value = answer(asyncio.run(call_http.call_http(Server(), Request('/toy/call/phone/pair', 'POST'))))
    assert status == 200 and value['data'] == {'url': 'https://bside-moon.cn/call/#pair=' + 'c' * 24, 'expires_in': 600}
    assert sent == {'method': 'POST', 'path': '/v1/call/pair', 'headers': {'Authorization': 'Bearer secret'}}
    assert 'secret' not in json.dumps(value)


def test_a_paired_phone_opens_the_line_and_can_be_unpaired(monkeypatch):
    account(monkeypatch)
    calls, started = [], []

    async def cloud(method, path, *, timeout, headers=None, body=None):
        calls.append((method, path))
        if method == 'DELETE':
            return 200, {'removed': 1}
        return 200, {'phones': [{'id': 'a1b2c3d4e5f6', 'label': 'iPhone · Safari', 'created': 1.0, 'used': None}], 'desk': False}
    monkeypatch.setattr(call_http, '_cloud_json', cloud)
    monkeypatch.setattr(desk, 'running', lambda: bool(started))
    monkeypatch.setattr(desk, 'start', lambda server, app: started.append(app))
    status, value = answer(asyncio.run(call_http.call_http(Server(), AppRequest('/toy/call/phones'))))
    assert status == 200 and value['data']['phones'][0]['label'] == 'iPhone · Safari' and value['data']['line'] is True
    assert len(started) == 1
    status, _ = answer(asyncio.run(call_http.call_http(Server(), Request('/toy/call/phone/unpair', 'POST', {'id': '../x'}))))
    assert status == 400
    status, value = answer(asyncio.run(call_http.call_http(Server(), Request('/toy/call/phone/unpair', 'POST', {'id': 'a1b2c3d4e5f6'}))))
    assert status == 200 and calls[-1] == ('DELETE', '/v1/call/phones?id=a1b2c3d4e5f6')


def line(monkeypatch, pickup='answer'):
    monkeypatch.setattr(call_context, 'build', lambda server, backend: ({'persona': 'p', 'world': {'now': 'x'}}, pickup))
    server = Server()
    return desk.Line(server, None, FakeWS()), server


def test_the_computer_answers_a_ring_with_the_call_it_builds(monkeypatch):
    async def scenario():
        ring, server = line(monkeypatch, 'sleepy')
        await ring.answer('r1')
        assert ring.ws.sent == [{'type': 'answer', 'ring': 'r1', 'context': {'persona': 'p', 'world': {'now': 'x'}},
                                 'pickup': 'sleepy'}]
        await ring.handle({'type': 'started', 'ring': 'r1', 'call_id': 'c' * 32})
        assert ring.calls['c' * 32]['pickup'] == 'sleepy' and server._call_seen_at > 0
        ring.calls['c' * 32]['world'].cancel()
    asyncio.run(scenario())


def test_she_misses_a_phone_call_in_the_bath(monkeypatch):
    async def scenario():
        ring, server = line(monkeypatch, 'missed')
        await ring.answer('r1')
        assert ring.ws.sent == [{'type': 'missed', 'ring': 'r1'}]
        assert server.store.calls[0]['missed'] is True
    asyncio.run(scenario())


def test_memories_for_what_the_user_says_are_sent_once(monkeypatch):
    monkeypatch.setattr(call_context, 'recall', lambda backend, text: ['他喜欢下雨天', '他养了一只猫'])
    monkeypatch.setattr(desk, '_backend', lambda app: object())

    async def scenario():
        ring, _ = line(monkeypatch)
        ring.calls['c' * 32] = {'pickup': 'answer', 'recalled': set(), 'world': asyncio.get_running_loop().create_future()}
        await ring.recall('c' * 32, '今天又下雨了')
        await ring.recall('c' * 32, '今天又下雨了')
        assert ring.ws.sent == [{'type': 'recall', 'call_id': 'c' * 32, 'memories': ['他喜欢下雨天', '他养了一只猫']}]
    asyncio.run(scenario())


def test_the_end_report_of_a_phone_call_is_kept_and_acknowledged(monkeypatch):
    absorbed = []
    monkeypatch.setattr(call_life, 'absorb_later', lambda server, row: absorbed.append(row['call_id']))

    async def scenario():
        ring, server = line(monkeypatch)
        world = asyncio.get_running_loop().create_future()
        ring.calls['d' * 32] = {'pickup': 'brief', 'recalled': set(), 'world': world}
        report = {'type': 'end', 'call_id': 'd' * 32, 'seconds': 90, 'charge_cents': 60, 'reason': 'hangup',
                  'transcript': [{'role': 'her', 'text': '嘘，我在上课。'}], 'notes': {'summary': '她在上课。'}}
        await ring.handle({'type': 'end', 'report': report})
        await ring.handle({'type': 'end', 'report': report})        # resent after a reconnect: kept once
        assert [r['call_id'] for r in server.store.calls] == ['d' * 32] and server.store.calls[0]['pickup'] == 'brief'
        assert ring.ws.sent == [{'type': 'kept', 'call_id': 'd' * 32}] * 2
        assert world.cancelled() and absorbed and server._call_seen_at == 0
    asyncio.run(scenario())


def test_the_line_stops_without_an_account(monkeypatch):
    monkeypatch.delenv('OLIVIA_GPU_API_URL', raising=False)
    monkeypatch.delenv('OLIVIA_GPU_API_KEY', raising=False)
    assert asyncio.run(asyncio.wait_for(desk._run(Server(), None), 2)) is None


@pytest.mark.parametrize('status,stops', [(301, True), (404, True), (401, True), (502, False)])
def test_a_service_without_phone_calls_ends_the_line_but_an_outage_is_retried(monkeypatch, status, stops):
    from aiohttp import RequestInfo, WSServerHandshakeError
    from multidict import CIMultiDictProxy, CIMultiDict
    from yarl import URL
    account(monkeypatch)
    sleeps = []

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def ws_connect(self, url, **kwargs):
            info = RequestInfo(URL(url), 'GET', CIMultiDictProxy(CIMultiDict()), URL(url))
            raise WSServerHandshakeError(info, (), status=status)

    async def sleep(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError
    monkeypatch.setattr(desk, 'ClientSession', Session)
    monkeypatch.setattr(desk.asyncio, 'sleep', sleep)

    async def scenario():
        try:
            await desk._run(Server(), None)
        except asyncio.CancelledError:
            pass
    asyncio.run(scenario())
    assert (sleeps == []) is stops


def test_the_call_page_offers_the_phone_with_a_qr_code():
    from runtime.call_ui import JAVASCRIPT
    assert 'function qrMatrix' in JAVASCRIPT and '/*QR*/' not in JAVASCRIPT
    assert "'/toy/call/phone/pair'" in JAVASCRIPT and '用手机打' in JAVASCRIPT
    from pathlib import Path
    from installer.start_local import _frontend_patcher_files
    source = Path(__file__).resolve().parents[2]
    assert 'runtime/call/qr.py' in {p.relative_to(source).as_posix() for p in _frontend_patcher_files(source)}


def _format_bits(grid):
    size = len(grid)
    bits = [grid[i][8] for i in range(6)] + [grid[7][8], grid[8][8], grid[8][7]] + [grid[8][14 - i] for i in range(9, 15)]
    second = [grid[8][size - 1 - i] for i in range(8)] + [grid[size - 15 + i][8] for i in range(8, 15)]
    return sum(b << i for i, b in enumerate(bits)), sum(b << i for i, b in enumerate(second))


def test_the_pairing_qr_code_is_well_formed():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is unavailable')
    from runtime.call.qr import JAVASCRIPT
    url = 'https://bside-moon.cn/call/#pair=' + 'Ab9_-x' * 4
    script = JAVASCRIPT + ';console.log(JSON.stringify(qrMatrix(%s).map(r=>r.map(c=>c?1:0))));' % json.dumps(url)
    grid = json.loads(subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=30, check=True).stdout)
    size = len(grid)
    assert size == 33 and all(len(row) == size for row in grid)            # version 4
    for x0, y0 in ((0, 0), (size - 7, 0), (0, size - 7)):                  # finder patterns
        ring = [grid[y0][x0 + i] for i in range(7)] + [grid[y0 + 6][x0 + i] for i in range(7)]
        assert all(ring) and all(grid[y0 + 2 + j][x0 + 2 + i] for i in range(3) for j in range(3))
        assert not any(grid[y0 + 1][x0 + 1 + i] for i in range(5))
    first, second = _format_bits(grid)
    assert first == second
    data = (first ^ 0x5412) >> 10
    assert data >> 3 == 0                                                  # error correction level M
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    assert ((data << 10) | rem) ^ 0x5412 == first                          # a valid BCH code word
    assert grid[size - 8][8] == 1                                          # the dark module


def test_pairing_tries_twice_on_a_slow_network(monkeypatch):
    from aiohttp import ClientError
    account(monkeypatch)
    attempts = []

    async def cloud(method, path, *, timeout, headers=None, body=None):
        attempts.append(timeout.total)
        if len(attempts) == 1:
            raise ClientError('reset')
        return 200, {'url': 'https://bside-moon.cn/call/#pair=' + 'c' * 24, 'expires_in': 600}
    monkeypatch.setattr(call_http, '_cloud_json', cloud)

    async def no_wait(seconds):
        return None
    monkeypatch.setattr(call_http.asyncio, 'sleep', no_wait)
    status, value = answer(asyncio.run(call_http.call_http(Server(), Request('/toy/call/phone/pair', 'POST'))))
    assert status == 200 and attempts == [15, 15]


def test_a_network_hiccup_keeps_the_call_button():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is unavailable')
    from runtime.call_ui import JAVASCRIPT
    start = JAVASCRIPT.index('  let configRetry = null;')
    source = JAVASCRIPT[start:JAVASCRIPT.index('\n  }\n', JAVASCRIPT.index('async function refreshConfig', start)) + 4]
    script = r'''
const assert = require('node:assert/strict');
let config = null, answers = [], timers = [];
const entry = {ready: false, toggleAttribute(name, on) { this.ready = on; }};
const local = async () => { const next = answers.shift(); if (next instanceof Error) throw next; return next; };
const setTimeout = (fn, ms) => { timers.push(ms); return timers.length; };
const clearTimeout = () => {};
''' + source + r'''
(async () => {
  answers = [{available: true, phone: true}]; await refreshConfig(); assert.equal(entry.ready, true);
  answers = [{available: false, reason: 'network'}]; await refreshConfig(); assert.equal(entry.ready, true);   // kept
  assert.deepEqual(timers, [30000]);                                                                          // retried soon
  answers = [new Error('CALL_UNAVAILABLE')]; await refreshConfig(); assert.equal(entry.ready, true);
  answers = [{available: false, reason: 'account'}]; await refreshConfig(); assert.equal(entry.ready, false); // a real answer
  answers = [{available: true}]; await refreshConfig(); assert.equal(entry.ready, true);
})().catch(e => { console.error(e); process.exit(1); });
'''
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
