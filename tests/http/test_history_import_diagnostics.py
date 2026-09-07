import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from runtime.diagnostics.support_bundle import build_diagnostic_bundle, project_history_import


@pytest.mark.parametrize("running", [True, False])
def test_configured_diagnostic_collector_reads_live_history_progress_without_importing(monkeypatch, running):
    import original_client_server as server

    collectors, snapshots = [], []
    def progress():
        snapshots.append(True)
        return {"status": "RUNNING", "stage": "memory", "total": 142, "processed": 18,
                "content": "private-letter", "source_id": "private-source", "path": "C:/private/key.txt"}
    async def fallback(request):
        raise AssertionError("diagnostics must not invoke an import route")
    module = SimpleNamespace(handler=fallback, _official_import_progress_snapshot=progress,
        _local_import_task=SimpleNamespace(done=lambda: not running),
        _local_import_result={"code": 503, "message": "private-error-text", "data": {
            "status": "UNAVAILABLE", "error_code": "OFFLINE_HISTORY_MEMORY_WRITE_FAILED",
            "memory_migration": {"error_code": "MEM0_SOURCE_DEDUP_UNAVAILABLE", "source_id": "private-source"},
            "key": "private-secret", "content": "private-letter"}})
    monkeypatch.setattr(server, "mount_original_client_diagnostics_api", lambda app, collect, **kwargs: collectors.append(collect))
    server.create_configured_original_client_server_runtime(server_module=module, environ={})
    assert snapshots == []  # Captured lazily at export, without crawling storage.
    source = collectors[0]()
    assert snapshots == [True]
    expected = {"state": "running" if running else "unavailable", "stage": "memory", "total": 142, "processed": 18, "task_running": running}
    if not running:
        expected["error_code"] = "MEM0_SOURCE_DEDUP_UNAVAILABLE"
    assert source["health"]["checks"]["history_import"] == expected
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        assert len(archive.namelist()) == 8
        assert "media-provider-tail.jsonl" in archive.namelist()
        assert json.loads(archive.read("health.json"))["checks"]["history_import"] == expected
        assert all(b"private-" not in archive.read(name) for name in archive.namelist())
    module._local_import_task = None
    module._local_import_result = {"code": 0, "data": {"status": "APPLIED"}}
    assert collectors[0]()["health"]["checks"]["history_import"]["state"] == "completed"
    assert len(snapshots) == 2  # The same collector sees later runtime changes.


def test_history_import_projection_filters_dirty_values_at_bundle_boundary():
    from runtime.diagnostics.support_bundle import _project_health
    dirty = {"state": "private-letter", "stage": ["private-path"], "total": True,
             "processed": -1, "task_running": "private-key", "error_code": "C:/private/key.txt",
             "source_id": "private-source", "content": "private-letter"}
    assert project_history_import(dirty) == {"state": "unknown", "stage": "unknown"}
    assert _project_health({"status": "available", "checks": {"history_import": dirty}}) == {
        "status": "available", "checks": {"history_import": {"state": "unknown", "stage": "unknown"}}}
