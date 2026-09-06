import threading
import errno
import pytest
from types import SimpleNamespace

from runtime.memory.mem0_memory import DeferredConversationMemoryAdapter, Mem0Config, Mem0ConversationMemoryAdapter


def _join(adapter):
    thread = adapter._thread
    if thread:
        thread.join(2)
        assert not thread.is_alive()


def test_reconfigure_waits_for_timed_out_operation_before_closing_and_reopening(tmp_path):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    config = Mem0Config(enabled=True, data_root=tmp_path)
    backend = SimpleNamespace(vector_store=SimpleNamespace(client=SimpleNamespace(close=closed.set)))
    old = Mem0ConversationMemoryAdapter(backend, config)
    old.status = lambda: SimpleNamespace(status="available", enabled=True)
    deferred = DeferredConversationMemoryAdapter(config, lambda: old)
    deferred.start_initialization()
    _join(deferred)
    def operation():
        entered.set()
        release.wait(2)
        assert not closed.is_set()
    assert old._write_call.call(operation, timeout_seconds=0.01)[0] == "timeout"
    assert entered.is_set()
    factories = []
    def replacement():
        assert closed.is_set()
        factories.append(True)
        return SimpleNamespace(status=lambda: SimpleNamespace(status="available", enabled=True))
    deferred.reconfigure_from(DeferredConversationMemoryAdapter(config, replacement))
    deferred.start_initialization()
    assert not closed.wait(0.05)
    assert not factories
    release.set()
    _join(deferred)
    assert closed.is_set() and factories == [True]


def test_close_releases_delegate_and_obsolete_initialization_candidate(tmp_path):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    def factory():
        entered.set()
        release.wait(2)
        return SimpleNamespace(close=closed.set, status=lambda: SimpleNamespace(status="available", enabled=True))
    adapter = DeferredConversationMemoryAdapter(Mem0Config(enabled=True, data_root=tmp_path), factory)
    adapter.start_initialization()
    assert entered.wait(1)
    adapter.close()
    release.set()
    _join(adapter)
    assert closed.is_set()


def test_reconfigure_keeps_delegate_open_until_dispatched_call_returns(tmp_path):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    def read(**kwargs):
        entered.set()
        release.wait(2)
        assert not closed.is_set()
        return []
    config = Mem0Config(enabled=True, data_root=tmp_path)
    old = SimpleNamespace(close=closed.set, list_memories=read,
        status=lambda: SimpleNamespace(status="available", enabled=True))
    adapter = DeferredConversationMemoryAdapter(config, lambda: old)
    adapter.start_initialization()
    _join(adapter)
    call = threading.Thread(target=lambda: adapter.list_memories(user_id="local-user"))
    call.start()
    assert entered.wait(1)
    adapter.reconfigure_from(DeferredConversationMemoryAdapter(config,
        lambda: SimpleNamespace(status=lambda: SimpleNamespace(status="available", enabled=True))))
    adapter.start_initialization()
    assert not closed.wait(0.05)
    release.set()
    call.join(2)
    _join(adapter)
    assert closed.is_set()


@pytest.mark.parametrize("error,code", [
    (PermissionError("private path"), "MEM0_STORAGE_PERMISSION_DENIED"),
    (OSError(errno.ENOSPC, "private path"), "MEM0_STORAGE_FULL"),
    (RuntimeError("Storage folder private path is already accessed by another instance of Qdrant client."), "MEM0_STORAGE_LOCKED"),
    (RuntimeError("private key and content"), "MEM0_INITIALIZATION_FAILED"),
])
def test_initialization_failures_expose_only_fixed_metadata(tmp_path, error, code):
    def fail():
        raise error
    adapter = DeferredConversationMemoryAdapter(Mem0Config(enabled=True, data_root=tmp_path), fail)
    adapter.start_initialization()
    _join(adapter)
    assert adapter.status().reason_code == code
    assert "private" not in repr(adapter.status())
