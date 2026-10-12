"""Keep-alive connections for the relay's judgment and settlement calls.

A fresh HTTPS connection per call pays a TLS handshake each time, which is the
largest part of a judgment's client-side latency. This module keeps a few idle
connections per origin and lends them to worker threads. ``PooledOpener.open`` is
a drop-in for the urllib opener those callers used before: no proxy, no
redirects, the caller's timeout, the same TLS context, and an ``HTTPError`` for
every status outside 2xx.
"""
import http.client
import io
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

IDLE_SECONDS = 60.0
MAX_IDLE_PER_ORIGIN = 4
_ERROR_BODY_LIMIT = 65536
_USER_AGENT = 'Python-urllib/' + urllib.request.__version__


def _stale(error):
    """A failure that a connection closed by its peer produces before any answer."""
    return isinstance(error, (ConnectionError, ssl.SSLEOFError, ssl.SSLZeroReturnError,
                              http.client.BadStatusLine, http.client.CannotSendRequest,
                              http.client.NotConnected))


def _alive(connection):
    """True when an idle connection has neither been closed nor sent stray bytes."""
    sock = connection.sock
    if sock is None:
        return False
    try:
        sock.settimeout(0.0)
        try:
            sock.recv(1)
        except (BlockingIOError, ssl.SSLWantReadError):
            return True
        return False
    except (OSError, ValueError):
        return False


def _tls_context():
    from runtime.remote_generation import gpu_tls_context
    return gpu_tls_context()


class ConnectionPool:
    """Idle connections by origin; each lent connection belongs to one thread."""

    def __init__(self, *, idle_seconds=IDLE_SECONDS, max_idle=MAX_IDLE_PER_ORIGIN,
                 clock=time.monotonic, context_factory=_tls_context):
        self._idle_seconds = idle_seconds
        self._max_idle = max_idle
        self._clock = clock
        self._context_factory = context_factory
        self._lock = threading.Lock()
        self._idle = {}
        self.created = 0
        self.reused = 0

    def acquire(self, origin, timeout, fresh=False):
        """Return ``(connection, reused)``; a new connection connects on first use."""
        while not fresh:
            with self._lock:
                idle = self._idle.get(origin)
                candidate = idle.pop() if idle else None
            if candidate is None:
                break
            connection, since = candidate
            if self._clock() - since <= self._idle_seconds and _alive(connection):
                connection.timeout = timeout
                connection.sock.settimeout(timeout)
                with self._lock:
                    self.reused += 1
                return connection, True
            connection.close()
        scheme, host, port = origin
        if scheme == 'https':
            connection = http.client.HTTPSConnection(host, port, timeout=timeout,
                                                     context=self._context_factory())
        else:
            connection = http.client.HTTPConnection(host, port, timeout=timeout)
        with self._lock:
            self.created += 1
        return connection, False

    def release(self, origin, connection):
        surplus = []
        with self._lock:
            now = self._clock()
            idle = self._idle.setdefault(origin, [])
            idle.append((connection, now))
            while len(idle) > self._max_idle or now - idle[0][1] > self._idle_seconds:
                surplus.append(idle.pop(0)[0])
        for old in surplus:
            old.close()

    def close_all(self):
        with self._lock:
            pooled = [item[0] for idle in self._idle.values() for item in idle]
            self._idle.clear()
        for connection in pooled:
            connection.close()

    def idle_count(self):
        with self._lock:
            return sum(len(idle) for idle in self._idle.values())


class _Response:
    """A 2xx answer; closing it returns the connection once the body is consumed."""

    def __init__(self, response, connection, origin, pool, url):
        self._response = response
        self._connection = connection
        self._origin = origin
        self._pool = pool
        self._finished = False
        self.status = self.code = response.status
        self.reason = response.reason
        self.headers = self.msg = response.msg
        self.url = url

    def read(self, amount=None):
        return self._response.read(amount)

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def info(self):
        return self.headers

    def close(self):
        if self._finished:
            return
        self._finished = True
        response = self._response
        try:
            if not response.isclosed() and response.chunked:
                response.read(1)  # Reads the terminating chunk when the body is exhausted.
            reusable = response.isclosed() and not response.will_close
        except (OSError, http.client.HTTPException, ValueError):
            reusable = False
        if reusable:
            self._pool.release(self._origin, self._connection)
        else:
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class PooledOpener:
    """``open(request, timeout=...)`` over the shared connection pool."""

    def __init__(self, pool):
        self._pool = pool

    def open(self, request, timeout=None):
        parts = urllib.parse.urlsplit(request.full_url)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise urllib.error.URLError('unsupported URL')
        origin = (parts.scheme, parts.hostname.lower(),
                  parts.port or (443 if parts.scheme == 'https' else 80))
        headers = dict(request.header_items())
        if not any(name.lower() == 'user-agent' for name in headers):
            headers['User-agent'] = _USER_AGENT
        for attempt in range(2):
            connection, reused = self._pool.acquire(origin, timeout, fresh=attempt == 1)
            try:
                connection.request(request.get_method(), request.selector, request.data, headers)
            except Exception as error:
                connection.close()
                # A pooled connection the peer already closed: connect again once.
                # Callers only send operations the relay accepts again unchanged.
                if reused and _stale(error):
                    continue
                if isinstance(error, OSError):
                    raise urllib.error.URLError(error) from None
                raise
            try:
                response = connection.getresponse()
            except Exception as error:
                connection.close()
                if reused and _stale(error):
                    continue
                raise
            break
        answer = _Response(response, connection, origin, self._pool, request.full_url)
        if 200 <= response.status < 300:
            return answer
        try:
            body = response.read(_ERROR_BODY_LIMIT + 1)
        except (OSError, http.client.HTTPException):
            body = b''
        answer.close()
        raise urllib.error.HTTPError(request.full_url, response.status, response.reason,
                                     response.msg, io.BytesIO(body[:_ERROR_BODY_LIMIT]))


_POOL = ConnectionPool()


def shared_opener():
    """The opener every relay judgment and settlement call shares."""
    return PooledOpener(_POOL)


def close_idle_connections():
    _POOL.close_all()
