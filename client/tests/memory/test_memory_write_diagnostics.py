import asyncio
import time

import pytest

from runtime.diagnostics import failure_context
from runtime.diagnostics.support_bundle import _project_tail_record
from runtime.memory import mem0_memory
from runtime.memory.bounded_daemon_call import BoundedDaemonCall
from runtime.memory.conversation_memory_delivery import ConversationMemoryDeliveryCommitter
from runtime.memory.conversation_memory_runtime import ConversationMemoryRuntime
from conversation_memory_port import ConversationMemoryStatus, MemoryWriteResult, MemoryWriteStatus
from tests.memory.test_conversation_memory_outbox import _state, _outbox


@pytest.fixture(autouse=True)
def clean_diagnostics():
    failure_context._MEMORY_WRITE_SEEN.clear()
    failure_context._RECENT_FAILURES.clear()
    failure_context.set_failure_logger(None)
    yield
    failure_context._MEMORY_WRITE_SEEN.clear()
    failure_context._RECENT_FAILURES.clear()
    failure_context.set_failure_logger(None)


def test_a_failed_write_is_named_by_component_and_kind_never_by_its_text():
    locked = RuntimeError('Storage folder C:\\secret\\qdrant is already accessed by another instance of Qdrant client.')
    assert mem0_memory._write_failure_code(locked) == 'MEM0_STORAGE_LOCKED'
    assert mem0_memory._write_failure_code(PermissionError('C:\\secret')) == 'MEM0_STORAGE_PERMISSION_DENIED'
    code = mem0_memory._write_failure_code(ValueError('the user said something private'))
    assert code == 'MEM0_WRITE_APP_VALUE' and 'private' not in code
    assert mem0_memory._write_failure_code(None) == 'MEM0_WRITE_ACK_MISSING'


def test_the_write_worker_reports_what_raised_and_the_caller_still_sees_failed():
    seen = []
    call = BoundedDaemonCall(thread_name='test-write', on_failure=seen.append)

    def boom():
        raise ValueError('private text')
    assert call.call(boom, timeout_seconds=5) == ('failed', None)
    assert len(seen) == 1 and isinstance(seen[0], ValueError)

    def angry(_error):
        raise RuntimeError('a diagnostic must not matter')
    call = BoundedDaemonCall(thread_name='test-write-2', on_failure=angry)
    assert call.call(boom, timeout_seconds=5) == ('failed', None)
    assert BoundedDaemonCall(thread_name='plain').call(boom, timeout_seconds=5) == ('failed', None)


def test_a_failing_delivery_is_logged_at_its_first_tenth_and_hundredth_time_only():
    lines = []
    failure_context.set_failure_logger(lambda event, **fields: lines.append((event, fields)))
    for _ in range(100):
        failure_context.record_memory_write_failure('MEM0_WRITE_VECTOR_STORE_RUNTIME', RuntimeError('x'))
    assert [fields['count'] for _, fields in lines] == [1, 10, 100]
    event, fields = lines[0]
    assert event == 'memory_write_failed'
    assert fields['cause_code'] == 'MEM0_WRITE_VECTOR_STORE_RUNTIME' and fields['exception_type'] == 'RuntimeError'
    assert len(failure_context.failure_snapshot()) == 3
    assert failure_context.record_memory_write_failure('not a code', None) is None
    failure_context.record_memory_write_failure('MEM0_WRITE_ACK_MISSING', None)
    assert 'exception_type' not in failure_context.failure_snapshot()[-1]
    failure_context.record_memory_write_failure('MEM0_WRITE_APP_OTHER', type('Weird', (Exception,), {})('secret'))
    assert failure_context.failure_snapshot()[-1]['exception_type'] == 'OTHER'


def test_the_bundle_keeps_the_code_and_the_count_of_a_write_failure():
    record = _project_tail_record({'event': 'memory_write_failed', 'cause_code': 'MEM0_WRITE_VECTOR_STORE_RUNTIME',
                                   'exception_type': 'RuntimeError', 'failure_stage': 'internal', 'count': 10,
                                   'text': 'private'}, runtime=True)
    assert record['cause_code'] == 'MEM0_WRITE_VECTOR_STORE_RUNTIME' and record['count'] == 10
    assert record['exception_type'] == 'RuntimeError' and 'text' not in record


def test_exhausted_deliveries_get_one_more_try_a_day_while_the_provider_answers(tmp_path, monkeypatch):
    class Memory:
        calls = 0
        operation_pending = False

        def status(self):
            return ConversationMemoryStatus('available', True, 'mem0', 'qdrant-local')

        def remember_exchange(self, **kwargs):
            self.calls += 1
            return MemoryWriteResult(MemoryWriteStatus.UNAVAILABLE, kwargs['source_id'], error_code='MEM0_WRITE_FAILED')
    memory = Memory()
    _state(tmp_path / 'state.json')
    box = _outbox(tmp_path, ConversationMemoryDeliveryCommitter(memory))
    runtime = ConversationMemoryRuntime(box)
    for _ in range(4):
        asyncio.run(box.scan_once())
    assert box.health()['exhausted_count'] == 1 and memory.calls == 3

    runtime._reply_provider_ready = False
    runtime._regrant_exhausted()                       # the provider has not answered: nothing is granted
    assert box.health()['exhausted_count'] == 1
    runtime._reply_provider_ready = True
    runtime._regrant_exhausted()
    assert 'exhausted_count' not in box.health() and box.health()['pending_count'] == 1
    assert box.journal_path.with_name('memory-regrant.json').is_file()
    asyncio.run(box.scan_once())                       # one more try, which fails again
    assert memory.calls == 4 and box.health()['exhausted_count'] == 1

    runtime._regrant_exhausted()                       # the same day: not again
    assert box.health()['exhausted_count'] == 1
    real = time.time
    monkeypatch.setattr('runtime.memory.conversation_memory_runtime.time.time', lambda: real() + 25 * 3600)
    runtime._regrant_exhausted()
    assert 'exhausted_count' not in box.health()
