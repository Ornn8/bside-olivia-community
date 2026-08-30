from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import zipfile

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from jsonschema import Draft202012Validator

from original_client_diagnostics_api import mount_original_client_diagnostics_api
from original_client_server import (
    _diagnostic_source,
    _launcher_tail,
    create_original_client_server_runtime,
)
from original_client_companion_api import CompanionCapability, CompanionReadStatus


async def _client(app: web.Application) -> TestClient:
    client = TestClient(TestServer(app), cookie_jar=None)
    await client.start_server()
    return client


def _source() -> dict[str, object]:
    return {
        "summary": {"status": "available"},
        "health": {"status": "available", "checks": {}},
        "install": {"status": "available"},
        "tasks": {"status": "idle", "pending": 0, "items": []},
        "launcher_tail": [],
        "runtime_tail": [],
    }


def test_diagnostics_export_is_loopback_safe_and_downloadable() -> None:
    async def scenario() -> None:
        app = web.Application()
        mount_original_client_diagnostics_api(app, lambda: _source())
        client = await _client(app)
        try:
            response = await client.get(
                "/toy/diagnostics/export",
                headers={"Host": "localhost", "Origin": "http://localhost:3000"},
            )
            assert response.status == 200
            assert response.headers["Content-Type"] == "application/zip"
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["Content-Disposition"] == 'attachment; filename="olivia-diagnostic-bundle.zip"'
            bundle = await response.read()
            with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            manifest_schema = json.loads(
                Path("contracts/diagnostic_bundle_manifest.schema.json").read_text(
                    encoding="utf-8"
                )
            )
            Draft202012Validator(manifest_schema).validate(manifest)

            forbidden = await client.get(
                "/toy/diagnostics/export",
                headers={"Host": "evil.example", "Origin": "https://evil.example"},
            )
            assert forbidden.status == 403
            forbidden_payload = await forbidden.json()
            assert forbidden_payload == {
                "schema_version": "olivia.diagnostic-export.v1",
                "status": "FAILED",
                "error_code": "DIAGNOSTIC_HOST_FORBIDDEN",
            }
            error_schema = json.loads(
                Path("contracts/diagnostic_export_error.schema.json").read_text(
                    encoding="utf-8"
                )
            )
            Draft202012Validator(error_schema).validate(forbidden_payload)
        finally:
            await client.close()

    asyncio.run(scenario())


def test_launcher_tail_reads_recent_events_from_large_append_only_log(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    path = log_dir / "launcher.jsonl"
    lines = [
        json.dumps(
            {
                "event": "backend_ready",
                "attempt": 1,
                "padding": "x" * 160,
            },
            separators=(",", ":"),
        )
        for _ in range(6_000)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert path.stat().st_size > 1 << 20

    records = _launcher_tail(tmp_path)

    assert len(records) == 200
    assert all(record["event"] == "backend_ready" for record in records)


def test_diagnostic_export_is_registered_in_the_machine_contract_and_docs() -> None:
    from http_contract import contract_document, route_spec

    assert route_spec("/toy/diagnostics/export") == {
        "methods": ["GET"],
        "capability": "support.diagnostics",
        "state": "available",
        "read_only": True,
        "error_code": None,
        "evidence": "local-extension",
    }
    assert contract_document()["capabilities"]["support.diagnostics"] == {
        "status": "available",
        "provider": "local-allowlisted-zip",
        "probe": "user-triggered",
    }
    assert "/toy/diagnostics/export" in Path("docs/B02_HTTP_CONTRACT.md").read_text(
        encoding="utf-8"
    )
    error_docs = Path("docs/B02_ERROR_CODES.md").read_text(encoding="utf-8")
    for code in (
        "DIAGNOSTIC_HOST_FORBIDDEN",
        "DIAGNOSTIC_ORIGIN_FORBIDDEN",
        "DIAGNOSTIC_EXPORT_UNAVAILABLE",
    ):
        assert code in error_docs


def test_original_server_mounts_diagnostics_before_legacy_catch_all() -> None:
    async def fallback(_request: web.Request) -> web.Response:
        return web.Response(text="legacy-catch-all")

    async def scenario() -> None:
        runtime = create_original_client_server_runtime(fallback)
        client = await _client(runtime.app)
        try:
            response = await client.get(
                "/toy/diagnostics/export",
                headers={"Host": "localhost", "Origin": "http://localhost:3000"},
            )
            assert response.status == 200
            assert response.headers["Content-Type"] == "application/zip"
            assert await response.read() != b"legacy-catch-all"
        finally:
            await client.close()

    asyncio.run(scenario())


def test_safe_log_runtime_ring_is_bounded_and_drops_unknown_fields() -> None:
    import local_server

    for index in range(205):
        local_server._safe_log(
            "diagnostic_test",
            method="GET",
            path="/toy/diagnostics/export",
            error_code="DIAGNOSTIC_TEST",
            body=f"private-{index}",
            real_id=f"id-{index}",
        )

    records = local_server.runtime_diagnostic_event_snapshot()
    assert len(records) == 200
    assert records[-1] == {
        "event": "diagnostic_test",
        "method": "GET",
        "error_code": "DIAGNOSTIC_TEST",
    }
    assert all(
        "body" not in record and "real_id" not in record and "path" not in record
        for record in records
    )


def test_diagnostic_source_projects_profiles_setup_and_recent_task_states() -> None:
    class Backend:
        @staticmethod
        def read_status() -> CompanionReadStatus:
            return CompanionReadStatus(
                memory=CompanionCapability("available"),
                private_world=CompanionCapability("degraded", "PRIVATE_WORLD_DEGRADED"),
                candidates=CompanionCapability("available"),
            )

    class Setup:
        @staticmethod
        def status() -> dict[str, object]:
            return {
                "status": "READY",
                "setup_completed": True,
                "llm": {
                    "key_configured": True,
                    "base_url": "https://must-not-leak.example/v1",
                    "model": "must-not-leak",
                },
                "session_token": "must-not-leak",
            }

    def health(profile: str) -> dict[str, object]:
        state = "DEGRADED" if profile == "llm" else "HEALTHY"
        return {
            "code": 0,
            "data": {
                "status": state,
                "backend_id": "desktop-local",
                "contract_version": "2.0",
                "required_checks": {"example": "available"},
                "capabilities": {
                    "settings.video_reply": {"status": "available"},
                    "native.tts": {"status": "unavailable"},
                },
            },
        }

    collect = _diagnostic_source(
        Backend(),  # type: ignore[arg-type]
        setup_service=Setup(),  # type: ignore[arg-type]
        launcher_tail_provider=None,
        runtime_tail_provider=None,
        health_profile_provider=health,
        task_snapshot_provider=lambda: (
            {
                "letter_status": "FAILED",
                "error_code": "LLM_TIMEOUT",
                "media_status": "NOT_REQUESTED",
                "reply_mode": "text_letter",
                "retryable": True,
                "created_at": 0,
                "letter_id": "must-not-leak",
                "content": "must-not-leak",
            },
        ),
    )

    source = collect()
    assert "backend_id" not in source["summary"]  # type: ignore[operator]
    assert source["summary"]["contract_version"] == "2.0"  # type: ignore[index]
    assert source["health"]["checks"] == {  # type: ignore[index]
        "candidates": {"state": "available"},
        "memory": {"state": "available"},
        "native_tts": {"state": "unavailable"},
        "private_world": {
            "state": "degraded",
            "error_code": "PRIVATE_WORLD_DEGRADED",
        },
        "profile_asr": {"state": "available"},
        "profile_core": {"state": "available"},
        "profile_llm": {"state": "degraded"},
        "profile_memory": {"state": "available"},
        "settings_video_reply": {"state": "available"},
    }
    assert source["install"] == {  # type: ignore[index]
        "status": "available",
        "setup_completed": True,
        "key_configured": True,
    }
    assert source["tasks"] == {  # type: ignore[index]
        "status": "idle",
        "pending": 0,
        "items": [
            {
                "status": "failed",
                "error_code": "LLM_TIMEOUT",
                "media_status": "not_requested",
                "reply_mode": "text_letter",
                "retryable": True,
                "stage": "failed",
                "elapsed_bucket": "over_6h",
            }
        ],
    }


def test_settings_ui_exposes_download_only_diagnostics_action() -> None:
    script = Path("original_client_settings_ui.py").read_text(encoding="utf-8")

    assert 'const DIAGNOSTIC_EXPORT_PATH = "/toy/diagnostics/export";' in script
    assert "response.blob()" in script
    assert 'download = "olivia-diagnostic-bundle.zip"' in script
    assert "诊断与反馈" in script
    assert "requestMutation(DIAGNOSTIC_EXPORT_PATH" not in script


def test_diagnostics_modules_are_in_both_formal_packaging_allowlists() -> None:
    from installer.build_windows_setup import _is_release_file
    from installer.full_patch import PAYLOAD_REQUIRED_RELATIVE_FILES, PAYLOAD_REQUIRED_ROOT_FILES

    assert "original_client_diagnostics_api.py" in PAYLOAD_REQUIRED_ROOT_FILES
    assert "runtime/diagnostics/__init__.py" in PAYLOAD_REQUIRED_RELATIVE_FILES
    assert "runtime/diagnostics/support_bundle.py" in PAYLOAD_REQUIRED_RELATIVE_FILES
    assert _is_release_file("original_client_diagnostics_api.py")
    assert _is_release_file("runtime/diagnostics/support_bundle.py")
    assert '"original_client_diagnostics_api"' in Path("pyproject.toml").read_text(
        encoding="utf-8"
    )


def test_full_patch_copies_diagnostics_when_the_release_files_are_tracked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from installer import full_patch

    root = Path(__file__).parents[2]
    tracked = full_patch._git_tracked_payload_files(root)
    monkeypatch.setattr(
        full_patch,
        "_git_tracked_payload_files",
        lambda _root: tracked
        | {
            "original_client_diagnostics_api.py",
            "runtime/diagnostics/__init__.py",
            "runtime/diagnostics/support_bundle.py",
        },
    )

    destination = tmp_path / "local_backend"
    full_patch.copy_project_payload(root, destination)

    assert (destination / "original_client_diagnostics_api.py").is_file()
    assert (destination / "runtime" / "diagnostics" / "support_bundle.py").is_file()


def test_diagnostics_export_fails_closed_when_collection_raises() -> None:
    def broken() -> dict[str, object]:
        raise OSError("C:/private/log")

    async def scenario() -> None:
        app = web.Application()
        mount_original_client_diagnostics_api(app, broken)
        client = await _client(app)
        try:
            response = await client.get(
                "/toy/diagnostics/export",
                headers={"Host": "127.0.0.1", "Origin": "http://127.0.0.1:3000"},
            )
            assert response.status == 503
            assert await response.json() == {
                "schema_version": "olivia.diagnostic-export.v1",
                "status": "UNAVAILABLE",
                "error_code": "DIAGNOSTIC_EXPORT_UNAVAILABLE",
            }
        finally:
            await client.close()

    asyncio.run(scenario())
