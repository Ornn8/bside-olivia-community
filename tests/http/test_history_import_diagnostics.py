import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from runtime.diagnostics.support_bundle import build_diagnostic_bundle, project_history_import


@pytest.mark.parametrize("running", [True, False])
@pytest.mark.parametrize("stage", ["memory", "memory_wait"])
def test_configured_diagnostic_collector_reads_live_history_progress_without_importing(monkeypatch, running, stage):
    import original_client_server as server

    collectors, snapshots = [], []
    def progress():
        snapshots.append(True)
        return {"status": "RUNNING", "stage": stage, "total": 142, "processed": 18,
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
    expected = {"state": "running" if running else "unavailable", "stage": stage, "total": 142, "processed": 18, "task_running": running}
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


def test_memory_provider_failure_survives_bundle_export_without_private_fields():
    import local_server as server
    record = server._runtime_diagnostic_record("history_memory_failed", {
        "status": "FAILED", "error_code": "MEM0_PROVIDER_HTTP_401",
        "key": "private-secret", "content": "private-letter", "message": "private-url",
    })
    source = {"summary": {"status": "available"}, "health": {"status": "available", "checks": {}},
              "install": {"status": "available"}, "tasks": {"status": "available", "pending": 0, "items": []},
              "launcher_tail": [], "runtime_tail": [record]}
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        # Verify the export projection separately from current health, which may show a retry.
        projected = archive.read("runtime-tail.jsonl")
        assert b"MEM0_PROVIDER_HTTP_401" in projected
        assert b"private-" not in projected
