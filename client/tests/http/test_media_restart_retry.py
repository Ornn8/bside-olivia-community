"""Service restarts retry the same order; deliberate refusals remain prompt."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from runtime import remote_generation as remote
from runtime.cloud_service import CloudError


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=0, delays=[])
    async def sleep(delay):
        state.delays.append(delay)
        state.now += delay
    monkeypatch.setattr(remote, 'time', SimpleNamespace(monotonic=lambda: state.now))
    monkeypatch.setattr(remote, 'asyncio', SimpleNamespace(sleep=sleep))
    return state


@pytest.mark.parametrize('action', ['capabilities', 'submit'])
@pytest.mark.parametrize('failure', [502, 503, 504, 'disconnect'])
def test_restart_replays_identical_request(action, failure, clock):
    seen = []
    async def scenario():
        async def handler(request):
            seen.append((request.headers.get('Idempotency-Key'), await request.read()))
            if len(seen) == 1:
                if failure == 'disconnect':
                    request.transport.close()
                    return web.Response()
                return web.Response(status=failure, text='gateway restarting')
            return web.json_response({'kinds': ['tts'], 'shared_assets': []} if action == 'capabilities'
                                     else {'task_id': 'accepted-order', 'status': 'queued'})
        app = web.Application(); app.router.add_route('*', '/{path:.*}', handler)
        async with TestServer(app) as server:
            data = {} if action == 'capabilities' else {'request_id': 'same-order-123', 'kind': 'tts', 'input': {'text': '合成'}}
            result = await remote.RemoteGeneration(str(server.make_url('/')), 'synthetic').request(action, data)
            if action == 'submit':
                assert result['task_id'] == 'accepted-order'
                assert seen[0][0] == 'same-order-123'
                assert json.loads(seen[0][1]) == data
    asyncio.run(scenario())
    assert len(seen) == 2
    assert seen[0] == seen[1]


@pytest.mark.parametrize('body,status', [({'error': 'GPU_OFFLINE'}, 503),
    ({'error': {'code': 'GPU_OFFLINE'}}, 503), ({'detail': 'GPU_OFFLINE'}, 503),
    ({}, 401), ({}, 402), ({}, 429), ({}, 400)])
def test_deliberate_refusal_is_not_retried(body, status, clock):
    seen = []
    async def scenario():
        async def handler(request):
            seen.append(1)
            return web.json_response(body, status=status)
        app = web.Application(); app.router.add_post('/v1/tasks', handler)
        async with TestServer(app) as server:
            with pytest.raises(CloudError):
                await remote.RemoteGeneration(str(server.make_url('/')), 'synthetic').request(
                    'submit', {'request_id': 'same-order-123', 'kind': 'tts', 'input': {}})
    asyncio.run(scenario())
    assert seen == [1]
    assert clock.delays == []


def test_persistent_restart_stops_at_sixty_seconds(clock):
    seen = []
    async def scenario():
        async def handler(request):
            seen.append(clock.now)
            return web.Response(status=502)
        app = web.Application(); app.router.add_get('/v1/capabilities', handler)
        async with TestServer(app) as server:
            with pytest.raises(CloudError, match='GPU_REQUEST_FAILED'):
                await remote.RemoteGeneration(str(server.make_url('/')), 'synthetic').request('capabilities', {})
    asyncio.run(scenario())
    assert 50 <= clock.now <= 60
    assert 2 < len(seen) < 12
    assert clock.delays[:3] == [1, 2, 4]


@pytest.mark.parametrize('failure', [TimeoutError(), remote.ClientError(),
                                    remote.ClientSSLError(None, OSError()), asyncio.CancelledError()])
def test_transport_deadline_and_non_retryable_errors(monkeypatch, clock, failure):
    timeouts = []
    class Session:
        def __init__(self, **kwargs):
            timeouts.append(kwargs['timeout'].total)
            self.connector = kwargs['connector']
        async def __aenter__(self):
            await self.connector.close()
            clock.now += 3  # Failed connection time counts toward the same budget.
            raise failure
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(remote, 'ClientSession', Session)
    async def scenario():
        with pytest.raises(asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else CloudError):
            await remote.RemoteGeneration('http://127.0.0.1:1', 'synthetic').request('capabilities', {})
    asyncio.run(scenario())
    if isinstance(failure, (remote.ClientSSLError, asyncio.CancelledError)):
        assert len(timeouts) == 1
        assert clock.delays == []
    else:
        assert 50 <= clock.now <= 60
        assert len(timeouts) > 1
        assert timeouts[-1] < 20
