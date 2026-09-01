from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import original_client_server
from conversation_memory_admin import MemoryAdminStatus
from conversation_memory_port import (
    ConversationMemoryRecord,
    NullConversationMemoryPort,
)
from original_client_server import (
    create_configured_original_client_server_runtime,
    create_original_client_server_runtime,
)
from original_client_setup_api import LLMSetupService
from private_world_candidates import (
    CandidateStatus,
    CandidateType,
    PrivateWorldCandidate,
)
from private_world_ledger import SQLitePrivateWorldLedger
from private_world_port import (
    ContinuationAwareness,
    HomeAccess,
    LocalContinuationFact,
    PrivateWorldSnapshot,
)


TRUSTED_ORIGIN = "https://client.example"


def test_successful_original_sign_in_unlocks_initial_setup(tmp_path: Path) -> None:
    async def signed_in(_request: web.Request) -> web.Response:
        return web.json_response({"code": 0, "message": "ok", "data": {}})

    async def scenario() -> None:
        service = LLMSetupService(
            tmp_path,
            protect=lambda value: value,
            unprotect=lambda value: value,
            probe=lambda *_args: None,
        )
        runtime = create_original_client_server_runtime(
            signed_in,
            setup_service=service,
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(runtime.app)) as client:
            before = await client.get(
                "/toy/setup/status", headers={"Origin": TRUSTED_ORIGIN}
            )
            assert (await before.json())["show_initial_setup"] is False

            login = await client.post(
                "/toy/signIn", headers={"Origin": TRUSTED_ORIGIN}
            )
            assert login.status == 200

            after = await client.get(
                "/toy/setup/status", headers={"Origin": TRUSTED_ORIGIN}
            )
            assert (await after.json())["show_initial_setup"] is True

    asyncio.run(scenario())


def test_successful_sign_in_authorizes_on_demand_capability_install(tmp_path: Path) -> None:
    async def signed_in(_request: web.Request) -> web.Response:
        return web.json_response({"code": 0, "message": "ok", "data": {}})

    class Status:
        def to_dict(self):
            return {
                "schema_version": "olivia.capability-status.v2",
                "status": "UNAVAILABLE", "capability": "long_term_memory",
                "state": "missing", "phase": "idle", "downloaded_bytes": 0,
                "total_bytes": 100, "remaining_bytes": 100, "installed_bytes": 0,
                "install_locations": [
                    {"root": "installation_root", "relative_path": "runtime/mem0-site-packages"},
                    {"root": "local_data_root", "relative_path": "memory/model-cache"},
                ],
                "version": "fixture", "license_summary": "fixture",
                "requires_gpu": False,
            }

    class Installer:
        starts: list[str] = []

        def status(self): return Status()
        def start(self, *, source_mode, **_options):
            self.starts.append(source_mode)
            return "APPLIED"
        def pause(self): return "NOOP"
        def resume(self, *, source_mode): return "NOOP"
        def uninstall(self, *, remove_model): return "NOOP"

    async def scenario() -> None:
        service = LLMSetupService(
            tmp_path, protect=lambda value: value, unprotect=lambda value: value,
            probe=lambda *_args: None,
        )
        installer = Installer()
        runtime = create_original_client_server_runtime(
            signed_in, setup_service=service, capability_installer=installer,
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(runtime.app)) as client:
            await client.post("/toy/signIn", headers={"Origin": TRUSTED_ORIGIN})
            setup = await client.get(
                "/toy/setup/status", headers={"Origin": TRUSTED_ORIGIN}
            )
            token = (await setup.json())["session_token"]
            response = await client.post(
                "/toy/capabilities/mem0/action",
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    "X-Olivia-Capability-Action": "confirmed",
                    "X-Olivia-Setup-Session": token,
                },
                json={"action": "install", "source": "auto"},
            )
            assert response.status == 200
            assert installer.starts == ["auto"]

    asyncio.run(scenario())


def test_successful_sign_in_still_cannot_apply_unsigned_local_component_update(
    tmp_path: Path,
) -> None:
    async def signed_in(_request: web.Request) -> web.Response:
        return web.json_response({"code": 0, "message": "ok", "data": {}})

    class Updater:
        applied: list[Path] = []

        def apply(self, package: Path, _manifest_sha256: str):
            self.applied.append(package)
            return {"status": "APPLIED", "component": "local_backend", "version": "1.2.3"}

        def rollback(self):
            return {"status": "ROLLED_BACK", "component": "local_backend", "version": "1.2.2"}

    async def scenario() -> None:
        package = tmp_path / "update.oliviapatch"
        package.write_bytes(b"fixture")
        service = LLMSetupService(
            tmp_path, protect=lambda value: value, unprotect=lambda value: value,
            probe=lambda *_args: None,
        )
        updater = Updater()
        runtime = create_original_client_server_runtime(
            signed_in, setup_service=service, component_updater=updater,
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(runtime.app)) as client:
            await client.post("/toy/signIn", headers={"Origin": TRUSTED_ORIGIN})
            setup = await client.get(
                "/toy/setup/status", headers={"Origin": TRUSTED_ORIGIN}
            )
            token = (await setup.json())["session_token"]
            response = await client.post(
                "/toy/updates/local/action",
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    "X-Olivia-Update-Action": "confirmed",
                    "X-Olivia-Setup-Session": token,
                },
                json={
                    "action": "apply",
                    "package_path": str(package),
                    "manifest_sha256": "a" * 64,
                },
            )
            assert response.status == 503
            assert await response.json() == {
                "status": "FAILED",
                "error_code": "UPDATE_ACTION_UNAVAILABLE",
            }
            assert updater.applied == []

    asyncio.run(scenario())


def test_boolean_false_sign_in_code_does_not_unlock_initial_setup(tmp_path: Path) -> None:
    async def malformed_sign_in(_request: web.Request) -> web.Response:
        return web.json_response({"code": False, "message": "invalid", "data": {}})

    async def scenario() -> None:
        service = LLMSetupService(
            tmp_path,
            protect=lambda value: value,
            unprotect=lambda value: value,
            probe=lambda *_args: None,
        )
        runtime = create_original_client_server_runtime(
            malformed_sign_in,
            setup_service=service,
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(runtime.app)) as client:
            response = await client.post(
                "/toy/signIn", headers={"Origin": TRUSTED_ORIGIN}
            )
            assert response.status == 200
            status = await client.get(
                "/toy/setup/status", headers={"Origin": TRUSTED_ORIGIN}
            )
            assert (await status.json())["login_observed"] is False

    asyncio.run(scenario())


def test_configured_runtime_mounts_capability_from_installed_layout(
    tmp_path: Path, monkeypatch,
) -> None:
    local_root = tmp_path / "BSideOliviaLocal"
    patch_root = local_root / "install"
    data_root = patch_root / "data"
    python_executable = (
        local_root / "runtime" / "python-3.12.10-embed-amd64" / "python.exe"
    )
    data_root.mkdir(parents=True)
    python_executable.parent.mkdir(parents=True)
    python_executable.write_bytes(b"synthetic")
    backend_root = patch_root / "local_backend"
    packaged_installer = backend_root / "installer"
    packaged_installer.mkdir(parents=True)
    source_installer = Path(original_client_server.__file__).resolve().parent / "installer"
    for name in (
        "mem0-capability-manifest.json",
        "mem0-runtime-artifacts.json",
        "mem0-runtime-requirements.txt",
    ):
        shutil.copyfile(source_installer / name, packaged_installer / name)
    monkeypatch.setattr(original_client_server.sys, "executable", str(python_executable))
    monkeypatch.setattr(
        original_client_server, "__file__", str(backend_root / "original_client_server.py")
    )
    server = SimpleNamespace(
        handler=_fallback,
        TRUSTED_FRONTEND_ORIGINS=frozenset({TRUSTED_ORIGIN}),
    )

    runtime = create_configured_original_client_server_runtime(
        server_module=server,
        environ={
            "OLIVIA_INSTALL_ROOT": str(patch_root),
            "OLIVIA_LOCAL_DATA_ROOT": str(data_root),
        },
    )

    assert runtime.capability_installer is not None
    assert runtime.public_status()["capability_installer_mounted"] is True


class MemoryAdminFixture:
    def status(self) -> MemoryAdminStatus:
        return MemoryAdminStatus(
            "available",
            "fixture",
            True,
            1,
            0,
            0,
        )

    def list_memories(self, *, query=None, limit=100):
        assert query in {None, "东京"}
        assert 1 <= limit <= 100
        return (
            ConversationMemoryRecord(
                "memory.fixture.1",
                "用户现在住在东京北区。",
                "local-user",
                "reply.fixture.1",
                score=0.9,
                created_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
            ),
        )


class PrivateWorldFixture:
    def __init__(self) -> None:
        self._snapshot = PrivateWorldSnapshot(
            version=7,
            familiarity=72,
            trust=88,
            comfort=65,
            closeness=55,
            tension=10,
            relationship_stage="close",
            nickname_permissions=("小河豚",),
            home_access=HomeAccess.VISIT_ACCESS,
            continuation_facts=(
                LocalContinuationFact(
                    "trip.yunnan",
                    "林离已经决定去云南采风。",
                    ContinuationAwareness.CHARACTER_KNOWN,
                ),
            ),
        )

    def snapshot(self) -> PrivateWorldSnapshot:
        return self._snapshot

    def control_view(self):
        return self._snapshot.control_view()

    def character_view(self):
        return self._snapshot.character_view()


class CandidateFixture:
    def list_candidates(self, *, status=None, now=None):
        assert status is CandidateStatus.PENDING
        assert now is not None
        created = datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)
        return (
            PrivateWorldCandidate(
                "candidate.fixture.1",
                "letter.fixture.1",
                1,
                CandidateType.REPAIR,
                "这次交流可能构成关系修复。",
                0.8,
                CandidateStatus.PENDING,
                created,
                created + timedelta(days=7),
            ),
        )


async def _fallback(request: web.Request) -> web.Response:
    return web.json_response(
        {"fallback": request.path},
        headers={"Cache-Control": "no-store"},
    )


def test_companion_routes_precede_existing_catch_all_and_return_real_data() -> None:
    async def scenario() -> None:
        runtime = create_original_client_server_runtime(
            _fallback,
            memory_admin=MemoryAdminFixture(),
            private_world=PrivateWorldFixture(),
            candidates=CandidateFixture(),
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(runtime.app)) as client:
            status = await client.get(
                "/toy/companion/status",
                headers={"Origin": TRUSTED_ORIGIN},
            )
            assert status.status == 200
            status_payload = await status.json()
            assert status_payload["capabilities"]["memory"] == {
                "state": "available",
                "count": 1,
            }
            assert status_payload["capabilities"]["private_world"] == {
                "state": "available"
            }
            assert status_payload["capabilities"]["candidates"] == {
                "state": "available",
                "count": 1,
            }

            memories = await client.get(
                "/toy/companion/memory?query=%E4%B8%9C%E4%BA%AC&limit=5",
                headers={"Origin": TRUSTED_ORIGIN},
            )
            assert memories.status == 200
            memory_payload = await memories.json()
            assert memory_payload["memories"][0]["text"] == "用户现在住在东京北区。"
            assert "user_id" not in memory_payload["memories"][0]

            private_world = await client.get(
                "/toy/companion/private-world",
                headers={"Origin": TRUSTED_ORIGIN},
            )
            assert private_world.status == 200
            private_payload = await private_world.json()
            assert private_payload["version"] == 7
            assert private_payload["relationship_stage"] == "close"
            assert private_payload["levels"] == {
                "familiarity": "high",
                "trust": "high",
                "comfort": "medium",
                "closeness": "medium",
                "tension": "low",
            }
            assert private_payload["nickname_permissions"] == ["小河豚"]
            assert private_payload["home_access"] == "visit_access"
            for hidden in ("familiarity", "trust", "comfort", "closeness", "tension"):
                assert hidden not in {
                    key for key in private_payload if key != "levels"
                }

            candidates = await client.get(
                "/toy/companion/private-world/candidates?limit=5",
                headers={"Origin": TRUSTED_ORIGIN},
            )
            assert candidates.status == 200
            candidate_payload = await candidates.json()
            assert candidate_payload["candidates"] == [
                {
                    "candidate_id": "candidate.fixture.1",
                    "candidate_type": "repair",
                    "summary": "这次交流可能构成关系修复。",
                    "created_at": "2026-08-23T01:00:00+00:00",
                    "expires_at": "2026-08-30T01:00:00+00:00",
                }
            ]

            fallback = await client.get("/health")
            assert fallback.status == 200
            assert await fallback.json() == {"fallback": "/health"}

    asyncio.run(scenario())


def test_configured_runtime_reuses_existing_memory_and_private_world_storage(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        root = tmp_path / "data"
        root.mkdir()
        database = root / "private_world" / "private_world.sqlite3"
        ledger = SQLitePrivateWorldLedger(database)
        builder = SimpleNamespace(
            conversation_memory=NullConversationMemoryPort(),
            conversation_memory_user_id="local-user",
        )
        lifecycle_apps = []
        server = SimpleNamespace(
            handler=_fallback,
            letters_adapter=SimpleNamespace(memory_prompt_builder=builder),
            private_world_port=ledger,
            private_world_committer=object(),
            TRUSTED_FRONTEND_ORIGINS=frozenset({TRUSTED_ORIGIN}),
            install_reply_task_lifecycle=lifecycle_apps.append,
        )
        runtime = create_configured_original_client_server_runtime(
            server_module=server,
            environ={
                "OLIVIA_LOCAL_DATA_ROOT": str(root),
                "OLIVIA_PRIVATE_WORLD_ENABLED": "1",
                "OLIVIA_PRIVATE_WORLD_DB": str(database),
            },
        )

        assert runtime.memory_admin is not None
        assert lifecycle_apps == [runtime.app]
        assert runtime.private_world_read is not None
        assert runtime.candidate_store is not None
        assert (root / "memory" / "memory_admin_audit.sqlite3").is_file()
        assert runtime.public_status() == {
            "status": "available",
            "network_scope": "loopback",
            "original_client_only": True,
            "memory_admin_mounted": True,
            "private_world_mounted": True,
            "candidate_store_mounted": True,
            "capability_installer_mounted": False,
        }

        async with TestClient(TestServer(runtime.app)) as client:
            response = await client.get(
                "/toy/companion/status",
                headers={"Origin": TRUSTED_ORIGIN},
            )
            assert response.status == 200
            payload = await response.json()
            assert payload["capabilities"]["memory"]["state"] == "disabled"
            assert payload["capabilities"]["private_world"]["state"] == "available"
            assert payload["capabilities"]["candidates"] == {
                "state": "available",
                "count": 0,
            }

    asyncio.run(scenario())


def test_configured_runtime_degrades_optional_services_without_losing_toy_api(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        server = SimpleNamespace(
            handler=_fallback,
            letters_adapter=SimpleNamespace(
                memory_prompt_builder=SimpleNamespace(
                    conversation_memory=None,
                    conversation_memory_user_id="local-user",
                )
            ),
            private_world_port=None,
            private_world_committer=None,
            TRUSTED_FRONTEND_ORIGINS=frozenset({TRUSTED_ORIGIN}),
        )
        runtime = create_configured_original_client_server_runtime(
            server_module=server,
            environ={"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path / "missing")},
        )
        async with TestClient(TestServer(runtime.app)) as client:
            status = await client.get(
                "/toy/companion/status",
                headers={"Origin": TRUSTED_ORIGIN},
            )
            payload = await status.json()
            assert payload["capabilities"]["memory"]["state"] == "disabled"
            assert payload["capabilities"]["private_world"]["state"] == "disabled"
            assert payload["capabilities"]["candidates"]["state"] == "disabled"

            fallback = await client.get("/toy/signIn")
            assert fallback.status == 200
            assert await fallback.json() == {"fallback": "/toy/signIn"}

    asyncio.run(scenario())
