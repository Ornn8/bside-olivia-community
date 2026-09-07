import asyncio

import pytest

from tests.memory.test_conversation_memory_outbox import _state, _outbox
from runtime.memory.conversation_memory_delivery import ConversationMemoryDeliveryCommitter
from runtime.memory.conversation_memory_runtime import ConversationMemoryRuntime
from conversation_memory_port import ConversationMemoryStatus, MemoryWriteResult, MemoryWriteStatus


@pytest.mark.parametrize('available', [True, False])
def test_exhausted_only_reply_readiness_is_cached_and_does_not_replay(tmp_path, monkeypatch, available):
    class Memory:
        calls = 0
        probes = 0
        operation_pending = False
        available = True
        def status(self):
            self.probes += 1
            return ConversationMemoryStatus('available' if self.available else 'unavailable', True, 'mem0', 'qdrant-local')
        def remember_exchange(self, **kwargs):
            self.calls += 1
            return MemoryWriteResult(MemoryWriteStatus.UNAVAILABLE, kwargs['source_id'], error_code='MEM0_WRITE_FAILED')
    memory = Memory()
    _state(tmp_path / 'state.json')
    box = _outbox(tmp_path, ConversationMemoryDeliveryCommitter(memory))
    runtime = ConversationMemoryRuntime(box)
    class Alive:
        def is_alive(self): return True
    runtime._thread = Alive()
    for _ in range(4): asyncio.run(box.scan_once())
    memory.available = available
    runtime._refresh_reply_provider()
    status = runtime.status()
    assert status.reply_ready is available
    assert status.status == 'degraded'
    assert status.reason_code == 'MEMORY_OUTBOX_RETRY_EXHAUSTED'
    probes = memory.probes
    for _ in range(3):
        runtime.status()
        assert runtime.reply_readiness_status().reply_ready is available
    assert memory.probes == probes
    assert memory.calls == 3
    assert box.health()['pending_count'] == box.health()['exhausted_count'] == 1
    original_health = box.health
    monkeypatch.setattr(box, 'health', lambda: dict(original_health(), pending_count=2))
    assert not runtime.status().reply_ready
    monkeypatch.setattr(box, 'health', original_health)
    runtime.status()
    memory.operation_pending = True
    assert not runtime.reply_readiness_status().reply_ready
    memory.operation_pending = False
    runtime._reply_provider_ready = False
    assert not runtime.reply_readiness_status().reply_ready
    runtime._refresh_reply_provider()
    runtime.status()
    (tmp_path / 'state.json').write_text('invalid json', encoding='utf-8')
    monkeypatch.setattr(runtime._stop_event, 'wait', lambda seconds: True)
    runtime._run()
    assert not runtime.reply_readiness_status().reply_ready
    _state(tmp_path / 'state.json')
    runtime._refresh_reply_provider()
    runtime.status()
    monkeypatch.setattr('runtime.memory.conversation_memory_runtime.time.monotonic', lambda: runtime._reply_provider_checked_at + 16)
    assert not runtime.reply_readiness_status().reply_ready
    box.retry_exhausted_once()
    assert not runtime.status().reply_ready
    runtime._thread = None
    assert not runtime.reply_readiness_status().reply_ready
