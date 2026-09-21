import asyncio
import json
import uuid

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from llm_gateway import GatewayError, OpenAICompatibleAdapter
from tests.llm.test_gateway import make_config, ROOT_MESSAGES


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('status,code,expected', [
    (429, 'insufficient_balance', 'PROVIDER_QUOTA_EXHAUSTED'),
    (502, 'upstream_unavailable_usage_pending', 'PROVIDER_USAGE_PENDING'),
    (409, 'request_already_submitted', 'PROVIDER_REQUEST_DUPLICATE'),
    (401, 'invalid_api_key', 'PROVIDER_AUTH_FAILED'),
    (502, 'upstream_rejected', 'PROVIDER_REJECTED'),
])
def test_relay_terminal_errors_keep_trace_and_never_retry(monkeypatch, stream, status, code, expected):
    monkeypatch.setenv('B03_TEST_KEY', 'synthetic')
    trace = str(uuid.uuid4())
    async def exercise():
        calls = []
        async def handler(request):
            calls.append(1)
            return web.json_response({'error': {'code': code, 'message': 'private-placeholder'}},
                                     status=status, headers={'X-Request-ID': trace})
        app = web.Application()
        app.router.add_post('/v1/chat/completions', handler)
        async with TestClient(TestServer(app)) as client:
            adapter = OpenAICompatibleAdapter(make_config(str(client.make_url('/v1')), max_retries=2))
            with pytest.raises(GatewayError) as caught:
                if stream:
                    _ = [x async for x in adapter.stream(ROOT_MESSAGES)]
                else:
                    await adapter.complete(ROOT_MESSAGES)
        assert caught.value.code == expected
        assert caught.value.retryable is False
        assert caught.value.provider_request_id == trace
        assert len(calls) == 1
        from runtime.diagnostics.failure_context import failure_snapshot
        from runtime.diagnostics.support_bundle import _project_tail
        exported = _project_tail(failure_snapshot(), runtime=True).decode()
        assert trace in exported
        assert 'private-placeholder' not in exported
    asyncio.run(exercise())


def test_public_error_preserves_actionable_category():
    import local_server
    assert local_server._public_llm_error('PROVIDER_QUOTA_EXHAUSTED') == ('LLM_QUOTA_EXHAUSTED', False)
    assert local_server._public_llm_error('PROVIDER_USAGE_PENDING') == ('LLM_USAGE_PENDING', False)
    from http_contract import LETTER_DETAIL_GENERATION_ERROR_CODES
    assert LETTER_DETAIL_GENERATION_ERROR_CODES['LLM_USAGE_PENDING']['retryable'] is False


def test_stream_error_after_stop_is_not_success(monkeypatch):
    monkeypatch.setenv('B03_TEST_KEY', 'synthetic')
    trace = str(uuid.uuid4())
    async def exercise():
        calls = []
        async def handler(request):
            calls.append(1)
            frames = [
                {'choices': [{'index': 0, 'delta': {'content': 'synthetic'}, 'finish_reason': 'stop'}]},
                {'error': {'code': 'usage_unavailable'}},
            ]
            text = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames)
            return web.Response(text=text + 'data: [DONE]\n\n', content_type='text/event-stream',
                                headers={'X-Request-ID': trace})
        app = web.Application()
        app.router.add_post('/v1/chat/completions', handler)
        async with TestClient(TestServer(app)) as client:
            adapter = OpenAICompatibleAdapter(make_config(str(client.make_url('/v1')), max_retries=2))
            with pytest.raises(GatewayError) as caught:
                _ = [x async for x in adapter.stream(ROOT_MESSAGES)]
        assert caught.value.code == 'PROVIDER_USAGE_PENDING'
        assert caught.value.provider_request_id == trace
        assert len(calls) == 1
    asyncio.run(exercise())


def test_retry_after_is_bounded_and_private_trace_is_dropped(monkeypatch):
    from types import SimpleNamespace
    from llm_gateway import _record_provider_failure
    from runtime.diagnostics.failure_context import failure_snapshot
    waits = []
    async def sleep(delay):
        waits.append(delay)
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    adapter = OpenAICompatibleAdapter(make_config('http://example.invalid/v1'))
    asyncio.run(adapter._retry_wait(0, SimpleNamespace(headers={'Retry-After': '99999'})))
    assert waits == [60]
    exc = GatewayError('PROVIDER_USAGE_PENDING', retryable=False)
    _record_provider_failure(exc, SimpleNamespace(headers={'X-Request-ID': 'private-not-a-uuid'}))
    assert 'provider_request_id' not in failure_snapshot()[-1]
    assert 'private-not-a-uuid' not in str(failure_snapshot()[-1])
