import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('age,valid_during_probe', [(0, True), (20, False)])
def test_provider_recheck_keeps_only_unexpired_success(monkeypatch, age, valid_during_probe):
    import runtime.memory.conversation_memory_runtime as module
    outbox = Mock(spec=module.CanonicalMemoryOutbox)
    outbox.committer = SimpleNamespace(delivery_pending=False, memory=object())
    outbox.health.return_value = {
        'status':'degraded', 'reason_code':'MEMORY_OUTBOX_RETRY_EXHAUSTED',
        'pending_count':1, 'exhausted_count':1,
    }
    runtime = module.ConversationMemoryRuntime(outbox)
    runtime._thread = SimpleNamespace(is_alive=lambda: True)
    runtime._reply_provider_ready = True
    runtime._reply_provider_checked_at = time.monotonic() - age
    runtime.status()
    observed = []

    def probe(_memory):
        observed.append(runtime.reply_readiness_status().reply_ready)
        return 'unavailable', 'MEM0_SEARCH_TIMEOUT'

    monkeypatch.setattr(module, '_provider_status', probe)
    runtime._refresh_reply_provider()
    assert observed == [valid_during_probe]
    assert runtime.reply_readiness_status().reply_ready is False
