from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aiohttp.test_utils import TestClient, TestServer
import pytest

from control_center.original_client_mutation_runtime import (
    OriginalClientMutationRuntimeError,
    mount_original_client_companion_mutations_from_app,
)
from original_client_companion_mutation_api import (
    CONFIRM_HEADER,
    CONFIRM_VALUE,
    MEMORY_DELETE_PATH,
)


class MemoryService:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def correct_memory(self, **kwargs):
        return {
            "status": "applied",
            "request_id": kwargs["request_id"],
            "affected_count": 2,
        }

    def delete_memory(self, **kwargs):
        self.deleted.append(kwargs["memory_id"])
        return {
            "status": "applied",
            "request_id": kwargs["request_id"],
            "affected_count": 1,
        }


class CandidateService:
    def decide_candidate(self, **kwargs):
        return {
            "status": "committed",
            "request_id": kwargs["request_id"],
            "affected_count": 1,
        }


@dataclass
class RuntimeOwner:
    memory: object | None = None
    candidate: object | None = None


def test_runtime_discovers_existing_services_in_bounded_app_graph() -> None:
    async def scenario() -> None:
        from aiohttp import web

        app = web.Application()
        memory = MemoryService()
        app[web.AppKey("fixture_owner", object)] = RuntimeOwner(
            memory=memory,
            candidate=CandidateService(),
        )
        mount_original_client_companion_mutations_from_app(app)

        async with TestClient(TestServer(app)) as client:
            origin = str(client.make_url("/")).rstrip("/")
            response = await client.post(
                MEMORY_DELETE_PATH,
                json={
                    "memory_id": "memory.fixture.1",
                    "request_id": "request.memory.delete.1",
                    "reason": "用户确认删除。",
                },
                headers={"Origin": origin, CONFIRM_HEADER: CONFIRM_VALUE},
            )
            assert response.status == 200
            assert (await response.json())["status"] == "APPLIED"
            assert memory.deleted == ["memory.fixture.1"]

    asyncio.run(scenario())


def test_runtime_mounts_honest_unavailable_endpoints_without_services() -> None:
    async def scenario() -> None:
        from aiohttp import web

        app = web.Application()
        mount_original_client_companion_mutations_from_app(app)
        async with TestClient(TestServer(app)) as client:
            origin = str(client.make_url("/")).rstrip("/")
            response = await client.post(
                MEMORY_DELETE_PATH,
                json={
                    "memory_id": "memory.fixture.1",
                    "request_id": "request.memory.delete.1",
                    "reason": "用户确认删除。",
                },
                headers={"Origin": origin, CONFIRM_HEADER: CONFIRM_VALUE},
            )
            assert response.status == 503
            assert (await response.json())["error_code"] == "MEMORY_MUTATION_DISABLED"

    asyncio.run(scenario())


def test_runtime_accepts_explicit_extra_root_without_storing_it_on_app() -> None:
    from aiohttp import web

    app = web.Application()
    memory = MemoryService()
    backend = mount_original_client_companion_mutations_from_app(
        app,
        extra_roots=(RuntimeOwner(memory=memory),),
    )
    assert backend is not None
    assert all(value is not memory for value in app.values())


def test_runtime_rejects_ambiguous_services() -> None:
    from aiohttp import web

    app = web.Application()
    with pytest.raises(OriginalClientMutationRuntimeError) as error:
        mount_original_client_companion_mutations_from_app(
            app,
            extra_roots=(MemoryService(), MemoryService()),
        )
    assert error.value.code == "ORIGINAL_COMPANION_MEMORY_SERVICE_AMBIGUOUS"


def test_runtime_opens_no_socket_and_creates_no_second_application(monkeypatch) -> None:
    from aiohttp import web

    app = web.Application()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("composition must not open a listener")

    monkeypatch.setattr(web, "run_app", forbidden)
    mount_original_client_companion_mutations_from_app(app)
