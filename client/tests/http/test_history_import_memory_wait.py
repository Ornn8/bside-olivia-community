import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("outcome", ["ready", "failed", "timeout", "cancel"])
def test_background_import_waits_before_starting_any_import(monkeypatch, outcome):
    import local_server

    status = SimpleNamespace(status="unavailable", reason_code="MEM0_INITIALIZING")
    monkeypatch.setattr(local_server, "conversation_memory_adapter", SimpleNamespace(status=lambda: status))
    monkeypatch.setattr(local_server, "_local_import_result", None)
    monkeypatch.setattr(local_server, "MEMORY_READY_REPLY_TIMEOUT_SECONDS", 0 if outcome == "timeout" else 120)
    progress = []
    calls = []
    monkeypatch.setattr(local_server, "_update_official_import_progress", lambda **kw: progress.append(kw))

    async def run():
        waiting = asyncio.Event()
        resume = asyncio.Event()

        async def sleep(_seconds):
            waiting.set()
            await resume.wait()

        async def route(*args, **kwargs):
            calls.append(status.reason_code)
            return {"code": 0, "data": {"status": "APPLIED"}}

        monkeypatch.setattr(local_server.asyncio, "sleep", sleep)
        monkeypatch.setattr(local_server, "route", route)
        task = asyncio.create_task(local_server._run_local_import())
        if outcome == "timeout":
            await task
            assert not calls
            assert local_server._local_import_result["data"]["error_code"] == "OFFICIAL_HISTORY_MEMORY_WAIT_TIMEOUT"
            return
        await asyncio.wait_for(waiting.wait(), 2)
        assert not calls
        assert progress[-1]["stage"] == "memory_wait"
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not calls
            return
        status.status = "available" if outcome == "ready" else "unavailable"
        status.reason_code = None if outcome == "ready" else "MEM0_INIT_BACKEND_LLM_CLIENT_UNEXPECTED"
        resume.set()
        await task
        # A terminal failure is handled by the existing route preflight, once.
        assert calls == [status.reason_code]

    asyncio.run(run())
