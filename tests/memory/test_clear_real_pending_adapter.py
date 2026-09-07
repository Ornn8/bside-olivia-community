import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conversation_memory_admin import ConversationMemoryAdminError, ConversationMemoryAdminService
from mem0_memory import Mem0Config, Mem0ConversationMemoryAdapter


def test_real_adapter_pending_write_rejects_clear_without_querying_locked_backend(tmp_path):
    reads, deletes = [], []
    release = threading.Event()
    backend = SimpleNamespace(get_all=lambda **kw: reads.append(kw), delete=lambda key: deletes.append(key))
    adapter = Mem0ConversationMemoryAdapter(backend, replace(Mem0Config(enabled=True, data_root=tmp_path), write_timeout_seconds=.1))
    service = ConversationMemoryAdminService(adapter, tmp_path / "admin.sqlite3")
    assert adapter._write_call.call(lambda: release.wait(2), timeout_seconds=.01)[0] == "timeout"
    try:
        with pytest.raises(ConversationMemoryAdminError, match="MEMORY_ADMIN_BUSY"):
            service.clear(request_id="synthetic-busy", reason="synthetic", confirmed=True)
        assert reads == deletes == []
        assert service._audit_row("synthetic-busy") is None
    finally:
        release.set()
        adapter._write_call.settle(timeout_seconds=1)
