import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('code', ['MEM0_EMBEDDING_CACHE_UNAVAILABLE', 'MEM0_IMPORT_FAILED'])
@pytest.mark.parametrize('path', ['/toy/letter/route-preview', '/toy/letter/send'])
def test_missing_memory_components_reject_before_acceptance(monkeypatch, code, path):
    import local_server as server
    monkeypatch.setattr(server.store, 'letters', [])
    status = SimpleNamespace(status='unavailable', reason_code=code)
    monkeypatch.setattr(server, 'conversation_memory_adapter', SimpleNamespace(status=lambda: status))
    before = list(server.store.letters)
    result = asyncio.run(server.route('POST', path, {'content':'synthetic new letter'}, {}))
    assert result['code'] == 503
    assert result['data']['error_code'] == code
    assert server.store.letters == before


def test_health_does_not_mask_missing_cache_with_stale_initializing(monkeypatch):
    import local_server as server
    from runtime.memory.mem0_memory import UnavailableConversationMemoryPort
    adapter = UnavailableConversationMemoryPort('MEM0_EMBEDDING_CACHE_UNAVAILABLE')
    monkeypatch.setattr(server, 'conversation_memory_adapter', adapter)
    monkeypatch.setattr(server.letters_adapter.memory_prompt_builder,
                        'conversation_runtime_status',
                        {'status':'unavailable', 'reason_code':'MEM0_INITIALIZING'})
    result = server._health_result()
    serialized = str(result)
    assert 'MEM0_EMBEDDING_CACHE_UNAVAILABLE' in serialized
    assert 'MEM0_INITIALIZING' not in serialized
