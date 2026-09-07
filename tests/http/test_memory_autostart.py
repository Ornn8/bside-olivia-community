"""The shipped original-client entrypoint warms memory without any UI requests."""
import asyncio
import threading
from types import SimpleNamespace


def test_configured_app_starts_memory_once_without_opening_settings(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    import local_server
    from original_client_server import create_configured_original_client_server_runtime
    from mem0_memory import DeferredConversationMemoryAdapter, Mem0Config

    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    calls, ready = [], []

    def factory():
        calls.append(True)
        entered.set()
        assert release.wait(3)
        return SimpleNamespace(
            status=lambda: SimpleNamespace(status="available", enabled=True),
            close=closed.set,
        )

    memory = DeferredConversationMemoryAdapter(Mem0Config(enabled=True, data_root=tmp_path), factory)
    monkeypatch.setattr(local_server, "conversation_memory_adapter", memory)
    monkeypatch.setattr(local_server, "_start_ready_conversation_memory_runtime", lambda: ready.append(True))
    monkeypatch.setattr(local_server, "_schedule_pending_reply_jobs", lambda: None)
    monkeypatch.setattr(local_server, "_schedule_pending_media_jobs", lambda: None)
    monkeypatch.setattr(local_server, "daily_life_runtime", None)
    monkeypatch.setattr(local_server, "stop_conversation_memory_runtime", lambda: None)

    async def exercise():
        runtime = create_configured_original_client_server_runtime(server_module=local_server, environ={})
        async with TestClient(TestServer(runtime.app)):
            # No GET or POST: reaching this point also proves slow factory work
            # does not block the actual configured app's startup.
            assert await asyncio.to_thread(entered.wait, 1)
            assert not release.is_set()
            assert not local_server._start_conversation_memory_initialization(asyncio.get_running_loop())
            assert calls == [True]
            release.set()
            for _ in range(100):
                if ready:
                    break
                await asyncio.sleep(.01)
            assert ready == [True]
        assert await asyncio.to_thread(closed.wait, 1)
        assert memory.closed

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        memory.close()
