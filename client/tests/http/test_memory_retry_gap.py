import asyncio

import pytest


@pytest.mark.parametrize('reason,pending,should_continue', [
    (None, 1, True),
    (None, 0, False),
    ('MEMORY_OUTBOX_RETRY_EXHAUSTED', 1, False),
    ('MEMORY_OUTBOX_STORAGE_UNAVAILABLE', 1, False),
])
def test_reply_deadline_does_not_fail_during_scheduled_memory_retry(
        monkeypatch, reason, pending, should_continue):
    import local_server as server
    from conversation_memory_runtime import ConversationMemoryRuntimeStatus
    letter = {'letter_id':'gap', 'letter_status':'PENDING', 'content':'synthetic'}
    monkeypatch.setattr(server.store, 'letters', [letter])
    monkeypatch.setattr(server, '_persist_store_state', lambda: None)
    ready = [False]
    generated = []
    monkeypatch.setattr(server, '_conversation_memory_ready_for_reply', lambda: ready[0])
    monkeypatch.setattr(server, 'conversation_memory_reply_readiness_status', lambda:
        ConversationMemoryRuntimeStatus('degraded', True, 'mem0-outbox', True,
            reason_code=reason, pending_count=pending, delivery_pending=False))

    async def sleep(_delay):
        assert letter['letter_status'] == 'PENDING'
        ready[0] = True  # Next maintenance scan settles the queued write.

    async def generate(*args, **kwargs):
        generated.append('once')
        return True

    monkeypatch.setattr(server.asyncio, 'sleep', sleep)
    monkeypatch.setattr(server, '_run_reply_job', generate)
    result = asyncio.run(server._run_reply_when_memory_ready(
        'gap', 'synthetic', idempotency_key=None, ready_timeout_seconds=0))
    assert result is should_continue
    assert generated == (['once'] if should_continue else [])
