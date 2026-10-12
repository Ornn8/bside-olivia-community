"""Relay calls share keep-alive connections without changing what a call means."""
import http.client
import http.server
import json
import ssl
import threading
import time
import urllib.error
import urllib.request

import pytest

from runtime.reply import jev_http


class Relay(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(('127.0.0.1', 0), Handler)
        self.connections = 0
        self.requests = []
        self.mode = 'ok'
        self.delay = 0.0
        self.lock = threading.Lock()

    @property
    def url(self):
        return 'http://127.0.0.1:%d' % self.server_address[1]


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        with self.server.lock:
            self.server.connections += 1

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        with self.server.lock:
            self.server.requests.append((self.path, dict(self.headers), body))
        mode = self.server.mode
        if mode == 'drop':
            self.close_connection = True
            return
        if self.server.delay:
            time.sleep(self.server.delay)
        if mode == 'redirect':
            self.send_response(302)
            self.send_header('Location', '/elsewhere')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        status = 200 if mode in ('ok', 'close', 'stale') else int(mode)
        payload = json.dumps({'echo': len(body)}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        if mode == 'close':
            self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(payload)
        if mode == 'stale':
            # The peer hangs up after answering without announcing it.
            self.close_connection = True


@pytest.fixture
def relay():
    server = Relay()
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def opener():
    pool = jev_http.ConnectionPool()
    yield jev_http.PooledOpener(pool)
    pool.close_all()


def post(opener, relay, body=b'{"q":1}', *, timeout=5, path='/v1/x', headers=None):
    request = urllib.request.Request(relay.url + path, data=body, method='POST',
                                     headers={'Content-Type': 'application/json', **(headers or {})})
    with opener.open(request, timeout=timeout) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == 'application/json'
        return json.loads(response.read(65537))


def test_sequential_calls_reuse_one_connection(relay, opener):
    for number in range(5):
        assert post(opener, relay, b'x' * number) == {'echo': number}
    assert relay.connections == 1
    assert opener._pool.created == 1 and opener._pool.reused == 4


def test_pool_is_shared_between_openers(relay):
    pool = jev_http.ConnectionPool()
    try:
        first, second = jev_http.PooledOpener(pool), jev_http.PooledOpener(pool)
        post(first, relay)
        post(second, relay)
        assert relay.connections == 1
    finally:
        pool.close_all()


def test_request_carries_the_callers_method_path_headers_and_body(relay, opener):
    post(opener, relay, b'{"k":"v"}', path='/v1/companion/semantic-decisions?a=1',
         headers={'X-Olivia-Request-Id': 'abc', 'Authorization': 'Bearer synthetic'})
    path, headers, body = relay.requests[0]
    assert path == '/v1/companion/semantic-decisions?a=1' and body == b'{"k":"v"}'
    lowered = {name.lower(): value for name, value in headers.items()}
    assert lowered['x-olivia-request-id'] == 'abc' and lowered['authorization'] == 'Bearer synthetic'
    assert lowered['content-type'] == 'application/json'
    assert lowered['user-agent'] == 'Python-urllib/' + urllib.request.__version__
    assert lowered['host'] == '127.0.0.1:%d' % relay.server_address[1]


def test_concurrent_workers_never_share_a_connection_in_use(relay, opener):
    results, errors = [], []

    def work(number):
        try:
            for _ in range(5):
                results.append(post(opener, relay, b'y' * number)['echo'] == number)
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=work, args=(number,)) for number in range(1, 9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not errors and results == [True] * 40
    assert relay.connections <= 8 and opener._pool.reused >= 40 - 8
    assert opener._pool.idle_count() <= jev_http.MAX_IDLE_PER_ORIGIN


def test_connection_the_peer_closed_while_idle_is_replaced_before_use(relay, opener):
    relay.mode = 'stale'
    post(opener, relay)
    relay.mode = 'ok'
    time.sleep(0.2)
    assert post(opener, relay) == {'echo': 7}
    assert relay.connections == 2 and len(relay.requests) == 2


def test_stale_pooled_connection_reconnects_once_and_replays_the_request(relay, opener, monkeypatch):
    relay.mode = 'stale'
    post(opener, relay)
    relay.mode = 'ok'
    time.sleep(0.2)
    # Skip the liveness probe so the closed connection is really used.
    monkeypatch.setattr(jev_http, '_alive', lambda connection: True)
    assert post(opener, relay) == {'echo': 7}
    assert relay.connections == 2
    # The closed connection never delivered the second request, so the server saw it once.
    assert len(relay.requests) == 2


def test_second_failure_is_raised_after_a_single_reconnect(relay, opener, monkeypatch):
    post(opener, relay)
    relay.mode = 'drop'
    monkeypatch.setattr(jev_http, '_alive', lambda connection: True)
    with pytest.raises((ConnectionError, http.client.HTTPException)):
        post(opener, relay)
    assert len(relay.requests) == 3  # one first call, then the pooled attempt and one reconnect


def test_a_fresh_connection_that_fails_is_not_retried(relay, opener):
    relay.mode = 'drop'
    with pytest.raises((ConnectionError, http.client.HTTPException)):
        post(opener, relay)
    assert len(relay.requests) == 1


def test_timeout_is_not_replayed(relay, opener):
    post(opener, relay)
    relay.delay = 1.0
    with pytest.raises(TimeoutError):
        post(opener, relay, timeout=0.2)
    assert len(relay.requests) == 2


def test_refused_connection_is_a_url_error_like_before(opener, monkeypatch):
    def refuse(self):
        raise ConnectionRefusedError('synthetic')
    monkeypatch.setattr(http.client.HTTPConnection, 'connect', refuse)
    request = urllib.request.Request('http://127.0.0.1:9/', data=b'{}', method='POST')
    with pytest.raises(urllib.error.URLError) as caught:
        opener.open(request, timeout=2)
    assert isinstance(caught.value.reason, ConnectionRefusedError)


def test_redirect_is_an_error_not_followed(relay, opener):
    relay.mode = 'redirect'
    with pytest.raises(urllib.error.HTTPError) as caught:
        post(opener, relay)
    assert caught.value.code == 302 and len(relay.requests) == 1
    caught.value.close()


@pytest.mark.parametrize('status', [401, 402, 429, 503])
def test_error_status_raises_http_error_with_readable_body_and_keeps_the_connection(relay, opener, status):
    relay.mode = str(status)
    with pytest.raises(urllib.error.HTTPError) as caught:
        post(opener, relay, b'abc')
    assert caught.value.code == status
    assert json.loads(caught.value.read(4097)) == {'echo': 3}
    caught.value.close()
    relay.mode = 'ok'
    post(opener, relay)
    assert relay.connections == 1


def test_connection_marked_close_is_not_pooled(relay, opener):
    relay.mode = 'close'
    post(opener, relay)
    post(opener, relay)
    assert relay.connections == 2 and opener._pool.idle_count() == 0


def test_partly_read_body_is_not_pooled(relay, opener):
    request = urllib.request.Request(relay.url + '/v1/x', data=b'zzzzzzzz', method='POST')
    with opener.open(request, timeout=5) as response:
        response.read(3)
    assert opener._pool.idle_count() == 0
    post(opener, relay)
    assert relay.connections == 2


def test_idle_connections_expire():
    clock = [0.0]
    pool = jev_http.ConnectionPool(idle_seconds=10, clock=lambda: clock[0])
    origin = ('http', '127.0.0.1', 9)
    first, reused = pool.acquire(origin, 1)
    assert not reused
    pool.release(origin, first)
    clock[0] = 11.0
    second, reused = pool.acquire(origin, 1)
    assert second is not first and not reused


def test_pool_keeps_a_bounded_number_of_idle_connections():
    pool = jev_http.ConnectionPool(max_idle=2)
    origin = ('http', '127.0.0.1', 9)
    connections = [pool.acquire(origin, 1)[0] for _ in range(5)]
    for connection in connections:
        pool.release(origin, connection)
    assert pool.idle_count() == 2


def test_https_connections_use_the_callers_tls_context():
    context = ssl.create_default_context()
    pool = jev_http.ConnectionPool(context_factory=lambda: context)
    connection, _ = pool.acquire(('https', 'api.example.invalid', 443), 5)
    assert isinstance(connection, http.client.HTTPSConnection) and connection._context is context
    assert connection.timeout == 5


def test_default_context_is_the_clients_verified_context(monkeypatch):
    sentinel = ssl.create_default_context()
    monkeypatch.setattr('runtime.remote_generation.gpu_tls_context', lambda: sentinel)
    pool = jev_http.ConnectionPool()
    connection, _ = pool.acquire(('https', 'api.example.invalid', 443), 5)
    assert connection._context is sentinel and sentinel.check_hostname


def test_only_http_and_https_are_opened(opener):
    with pytest.raises(urllib.error.URLError):
        opener.open(urllib.request.Request('file:///etc/passwd'), timeout=1)


def test_decision_ports_share_the_pool_and_keep_the_same_checks(monkeypatch):
    from runtime.reply.companion_decision import JevDecisionPort
    monkeypatch.setattr('runtime.remote_generation.gpu_tls_context', lambda: ssl.create_default_context())
    first = JevDecisionPort('http://127.0.0.1:8097/v1/companion/decide')
    second = JevDecisionPort('http://127.0.0.1:8097/v1/companion/decide')
    assert first._opener._pool is second._opener._pool is jev_http._POOL
