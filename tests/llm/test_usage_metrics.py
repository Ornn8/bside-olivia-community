import asyncio
import json
import sqlite3

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from llm_gateway import GatewayConfig, OpenAICompatibleAdapter, ProviderProtocolError
from runtime.diagnostics.usage_metrics import normalize_usage, record_usage


def test_memory_sdk_usage_is_counted_before_truncation_guard(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from runtime.memory.mem0_memory import _guard_extraction_client, Mem0AdapterError
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path))
    response = SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: {'prompt_tokens': 42, 'completion_tokens': 9}),
                               choices=[SimpleNamespace(finish_reason='length')])
    provider = SimpleNamespace(client=SimpleNamespace(base_url='https://example.invalid/v1', chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response))))
    _guard_extraction_client(provider)
    with pytest.raises(Mem0AdapterError):
        provider.client.chat.completions.create()
    with sqlite3.connect(tmp_path / 'diagnostics/token-usage.sqlite3') as db:
        assert db.execute("SELECT total FROM usage WHERE purpose='memory' AND metric='output_tokens'").fetchone()[0] == 9


def test_missing_usage_and_private_fields_are_not_counted_as_zero(tmp_path, monkeypatch):
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path))
    record_usage({'prompt_tokens': 100, 'prompt_cache_hit_tokens': 80, 'completion_tokens': 12,
                  'completion_tokens_details': {'reasoning_tokens': 10}, 'secret': 'PRIVATE'},
                 purpose='reply', outcome='response')
    record_usage(None, purpose='reply', outcome='response')
    path = tmp_path / 'diagnostics/token-usage.sqlite3'
    with sqlite3.connect(path) as db:
        rows = {metric: (total, samples) for metric, total, samples in db.execute('SELECT metric,total,samples FROM usage')}
    assert rows['attempts'] == (2, 2)
    assert rows['input_tokens'] == (100, 1)
    assert rows['uncached_tokens'] == (20, 1)
    assert rows['output_tokens'] == (12, 1)  # reasoning is a subset, never added twice
    assert b'PRIVATE' not in path.read_bytes()
    assert normalize_usage({'prompt_tokens': True})['input_tokens'] is None
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(path))  # storage error must not fail reply
    record_usage({}, purpose='reply', outcome='error')


def test_broken_sdk_usage_is_missing_not_a_generation_error():
    class Usage:
        def model_dump(self):
            raise RuntimeError('private SDK error')
    assert all(value is None for value in normalize_usage(Usage()).values())


def test_locked_accounting_is_reported_without_blocking_reply(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path))
    record_usage({}, purpose='reply', outcome='response')
    with sqlite3.connect(tmp_path / 'diagnostics/token-usage.sqlite3') as db:
        db.execute('BEGIN EXCLUSIVE')
        record_usage({}, purpose='reply', outcome='response')
    assert 'TOKEN_USAGE_WRITE_FAILED' in caplog.text


@pytest.mark.parametrize('stream', [False, True])
def test_network_retry_and_terminal_usage_are_recorded_once(tmp_path, monkeypatch, stream):
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path))
    async def exercise():
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return web.Response(status=503)
            usage = {'prompt_tokens': 100, 'completion_tokens': 5, 'prompt_cache_hit_tokens': 90}
            if not stream:
                return web.json_response({'choices': [{'finish_reason': 'length', 'message': {'content': 'PRIVATE'}}], 'usage': usage})
            events = [{'choices': [{'delta': {'content': 'hello'}, 'finish_reason': None}]},
                      {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
                      {'choices': [], 'usage': usage}]
            return web.Response(text=''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n', content_type='text/event-stream')
        app = web.Application()
        app.router.add_post('/chat/completions', handler)
        async with TestServer(app) as server:
            gateway = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible', model='test', base_url=str(server.make_url('/')).rstrip('/'), max_retries=1, retry_backoff_seconds=0))
            if stream:
                build_body = gateway._body
                gateway._body = lambda *args, **kwargs: {**build_body(*args, **kwargs), 'stream_options': {'include_usage': True}}
            messages = [{'role': 'user', 'content': 'PRIVATE'}]
            if stream:
                assert ''.join([d.text async for d in gateway.stream(messages, request_id='letter-reply:PRIVATE')]) == 'hello'
            else:
                with pytest.raises(ProviderProtocolError):
                    await gateway.complete(messages, request_id='letter-reply:PRIVATE')
        assert calls == 2
    asyncio.run(exercise())
    path = tmp_path / 'diagnostics/token-usage.sqlite3'
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT sum(total) FROM usage WHERE metric='attempts'").fetchone()[0] == 2
        assert db.execute("SELECT sum(total) FROM usage WHERE metric='cached_tokens'").fetchone()[0] == 90
    assert b'PRIVATE' not in path.read_bytes()


def test_missing_usage_tail_does_not_regenerate_finished_reply(monkeypatch):
    from aiohttp import StreamReader

    finished = set()
    timed_out = set()
    read_line = StreamReader.readline

    async def timeout_after_terminal(reader):
        # Inject the transport failure only after the consumer has processed
        # the completed body, not during connection startup under suite load.
        if reader in finished:
            timed_out.add(reader)
            raise asyncio.TimeoutError()
        line = await read_line(reader)
        if b'"finish_reason":"stop"' in line:
            finished.add(reader)
        return line

    monkeypatch.setattr(StreamReader, 'readline', timeout_after_terminal)
    async def exercise():
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            await response.write(b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":"stop"}]}\n\n')
            return response
        app = web.Application()
        app.router.add_post('/chat/completions', handler)
        async with TestServer(app) as server:
            gateway = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible', model='test', base_url=str(server.make_url('/')).rstrip('/'), max_retries=2))
            build_body = gateway._body
            gateway._body = lambda *args, **kwargs: {**build_body(*args, **kwargs), 'stream_options': {'include_usage': True}}
            assert ''.join([d.text async for d in gateway.stream([{'role':'user','content':'test'}])]) == 'hello'
            assert len(timed_out) == 1
        assert calls == 1
    asyncio.run(exercise())
