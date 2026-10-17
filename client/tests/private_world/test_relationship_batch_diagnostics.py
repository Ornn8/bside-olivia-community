"""HTTP failure diagnostics retain status without logging server bodies or credentials."""
import asyncio
import sys
from types import SimpleNamespace

import pytest

from runtime import relationship_batch as batch


@pytest.mark.parametrize('status,body,expected', [
    (301, b'<html>private redirect</html>', 'RELATIONSHIP_BATCH_UNAVAILABLE'),
    (429, b'{"error":{"code":"relationship_background_busy"}}', 'relationship_background_busy'),
    (502, b'{"error":{"code":"relationship_upstream_unavailable"}}', 'relationship_upstream_unavailable'),
    (500, b'{"error":{"code":"private_token_do_not_log"}}', 'RELATIONSHIP_BATCH_UNAVAILABLE'),
    (500, b'{"error":[]}', 'RELATIONSHIP_BATCH_UNAVAILABLE'),
    (500, b'x' * 10000, 'RELATIONSHIP_BATCH_UNAVAILABLE'),
], ids=['redirect', 'busy', 'upstream', 'unsafe-code', 'malformed', 'oversized'])
def test_request_status_and_safe_error_code(monkeypatch, status, body, expected):
    import aiohttp
    calls = []

    class Response:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def iter_chunked(self, size):
            for offset in range(0, len(body), size): yield body[offset:offset+size]

    response = Response()
    response.status, response.content = status, response

    class Session(Response):
        def __init__(self, **kwargs): pass
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return response

    monkeypatch.setattr(aiohttp, 'ClientSession', Session)
    monkeypatch.setitem(sys.modules, 'original_client_relay_api', SimpleNamespace(RELAY_BASE='https://synthetic.invalid/v1'))
    monkeypatch.setitem(sys.modules, 'runtime.tls', SimpleNamespace(client_tls_context=lambda: None))
    monkeypatch.setitem(sys.modules, 'runtime.cloud_policy', SimpleNamespace(get_policy=lambda key: {}))
    gateway = SimpleNamespace(config=SimpleNamespace(base_url='https://synthetic.invalid/v1', api_key_env='SYNTHETIC_BATCH_KEY'))
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(batch.request_batch(gateway, {'request_id': 'synthetic', 'turns': []}, 'friend'))
    assert caught.value.http_status == status
    assert caught.value.error_code == expected
    assert calls[0][1]['allow_redirects'] is False
    assert 'private' not in str(caught.value)


def test_tick_logs_status_and_code_and_defers(monkeypatch):
    from runtime import relationship_lite, relationship_growth, relationship_moments
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    deferred, logs = [], []
    queue = SimpleNamespace(sync=lambda *args: None, due=lambda now: True,
                            pending=lambda now: {'request_id': 'synthetic', 'turns': []},
                            result=lambda identity: None, defer=deferred.append)
    monkeypatch.setattr(batch, 'BatchQueue', lambda path: queue)
    monkeypatch.setattr(relationship_lite, 'ledger', lambda server: SimpleNamespace(_database_path='unused', snapshot=lambda: None))
    monkeypatch.setattr(relationship_growth, 'route', lambda snapshot: 'friend')
    monkeypatch.setattr(relationship_moments, 'sync_messages', lambda *args: None)
    server = SimpleNamespace(store=SimpleNamespace(letters=[],personal_chats=[]),
                             letters_adapter=SimpleNamespace(gateway=None),
                             _safe_log=lambda event, **fields: logs.append((event, fields)))
    async def fail(*args): raise batch.BatchRequestError(301, 'RELATIONSHIP_BATCH_UNAVAILABLE')
    assert asyncio.run(batch.tick(server, now, fail)) == 'deferred'
    assert deferred == [now]
    assert logs == [('relationship_batch_deferred', {'http_status': 301, 'status': 'HTTP_301',
                                                      'error_code': 'RELATIONSHIP_BATCH_UNAVAILABLE'})]
    async def secret_failure(*args): raise RuntimeError('private letter and bearer token')
    logs.clear()
    assert asyncio.run(batch.tick(server, now, secret_failure)) == 'deferred'
    assert logs[0][1] == {'http_status': 0, 'status': 'DEFERRED', 'error_code': 'RELATIONSHIP_BATCH_UNAVAILABLE'}
