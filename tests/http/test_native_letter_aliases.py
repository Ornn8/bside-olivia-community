from __future__ import annotations

import asyncio

import pytest


def _synthetic_letter() -> dict[str, object]:
    return {
        "letter_id": "synthetic-native-letter",
        "content": "synthetic mailbox content",
        "letter_status": "COMPLETED",
        "audit_status": 2,
        "reply_text": "synthetic mailbox reply",
        "reply_mode": "text_letter",
        "reply_not_before": 0.0,
        "media_status": "NOT_REQUESTED",
        "is_read": 0,
        "created_at": 1_700_000_000,
    }


def test_native_letter_list_alias_matches_toy_route_contract() -> None:
    import local_server

    native = asyncio.run(local_server.route("GET", "/letter/list", {}, {}))
    toy = asyncio.run(local_server.route("GET", "/toy/letter/list", {}, {}))

    assert native == toy


@pytest.mark.parametrize(
    ("method", "native_path", "toy_path", "body", "query"),
    (
        (
            "GET",
            "/letter/unread_count",
            "/toy/letter/unread_count",
            {},
            {"scope": "legacy"},
        ),
        (
            "GET",
            "/letter/detail",
            "/toy/letter/detail",
            {},
            {"letter_id": "synthetic-missing", "scope": "current"},
        ),
        (
            "POST",
            "/letter/resend",
            "/toy/letter/resend",
            {"letter_id": "synthetic-resend"},
            {},
        ),
        (
            "POST",
            "/letter/share",
            "/toy/letter/share",
            {"letter_id": "synthetic-share"},
            {},
        ),
    ),
)
def test_native_letter_aliases_preserve_route_inputs(
    method: str,
    native_path: str,
    toy_path: str,
    body: dict[str, object],
    query: dict[str, str],
) -> None:
    import local_server

    native = asyncio.run(local_server.route(method, native_path, body, query))
    toy = asyncio.run(local_server.route(method, toy_path, body, query))

    assert native == toy


def test_native_letter_send_alias_preserves_body_and_stays_deferred(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import local_server
    from runtime.video_reply_settings import VideoReplySettingsStore

    monkeypatch.setattr(local_server, "store", local_server.Store())
    monkeypatch.setattr(local_server, "_state_root", lambda: tmp_path)
    monkeypatch.setattr(
        local_server,
        "video_reply_settings_store",
        VideoReplySettingsStore.initialize(tmp_path),
    )
    scheduled: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        local_server,
        "_schedule_reply_job",
        lambda *args, **_kwargs: scheduled.append(args),
    )
    monkeypatch.setattr(
        local_server.letters_adapter,
        "reply",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("provider must remain deferred")
        ),
    )
    body = {
        "content": "synthetic native alias letter",
        "idempotency_key": "synthetic-native-alias",
    }

    native = asyncio.run(
        local_server.route(
            "POST",
            "/letter/send",
            body,
            {},
            defer_reply=True,
        )
    )
    toy = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            body,
            {},
            defer_reply=True,
        )
    )

    assert native == toy
    assert native["data"]["status"] == "PENDING"
    assert local_server.store.letters[0]["content"] == body["content"]
    assert len(scheduled) == 1


@pytest.mark.parametrize(
    ("method", "native_path", "toy_path", "body"),
    (
        ("GET", "/letter/list?scope=legacy", "/toy/letter/list?scope=legacy", None),
        ("GET", "/letter/unread_count", "/toy/letter/unread_count", None),
        (
            "GET",
            "/letter/detail?letter_id=synthetic-native-letter",
            "/toy/letter/detail?letter_id=synthetic-native-letter",
            None,
        ),
        (
            "POST",
            "/letter/resend",
            "/toy/letter/resend",
            {"letter_id": "synthetic-native-letter"},
        ),
        (
            "POST",
            "/letter/share",
            "/toy/letter/share",
            {"letter_id": "synthetic-native-letter"},
        ),
    ),
)
def test_public_runtime_native_alias_matches_toy_contract(
    method: str,
    native_path: str,
    toy_path: str,
    body: dict[str, object] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_server
    from aiohttp.test_utils import TestClient, TestServer
    from original_client_server import create_original_client_server_runtime

    isolated_store = local_server.Store()
    isolated_store.letters.append(_synthetic_letter())
    monkeypatch.setattr(local_server, "store", isolated_store)

    async def scenario() -> None:
        runtime = create_original_client_server_runtime(
            local_server.handler,
            letter_collection=local_server._letter_collection,
        )
        async with TestClient(TestServer(runtime.app, access_log=None)) as client:
            native = await client.request(method, native_path, json=body)
            native_payload = await native.json()
            toy = await client.request(method, toy_path, json=body)
            toy_payload = await toy.json()

            assert native.status == toy.status
            assert native_payload == toy_payload

    asyncio.run(scenario())


def test_public_handler_native_send_preserves_body_query_and_stays_deferred(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import local_server
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from runtime.video_reply_settings import VideoReplySettingsStore

    monkeypatch.setattr(local_server, "store", local_server.Store())
    monkeypatch.setattr(local_server, "_state_root", lambda: tmp_path)
    monkeypatch.setattr(
        local_server,
        "video_reply_settings_store",
        VideoReplySettingsStore.initialize(tmp_path),
    )
    monkeypatch.setattr(
        local_server,
        "_conversation_memory_ready_for_reply",
        lambda: True,
    )
    scheduled: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        local_server,
        "_schedule_reply_job",
        lambda *args, **_kwargs: scheduled.append(args),
    )

    async def provider_forbidden(*_args, **_kwargs):
        raise AssertionError("provider must remain deferred")

    monkeypatch.setattr(local_server, "generate_reply", provider_forbidden)

    async def scenario() -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app, access_log=None)) as client:
            native = await client.post(
                "/letter/send?request_id=synthetic-http-alias",
                json={"content": "synthetic HTTP alias letter"},
            )
            native_payload = await native.json()
            toy = await client.post(
                "/toy/letter/send?request_id=synthetic-http-alias",
                json={"content": "synthetic HTTP alias letter"},
            )
            toy_payload = await toy.json()

            assert native.status == toy.status == 200
            assert native_payload == toy_payload

    asyncio.run(scenario())
    assert local_server.store.letters[0]["content"] == "synthetic HTTP alias letter"
    assert len(scheduled) == 1


def test_public_runtime_native_send_matches_toy_contract_without_provider_call(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import local_server
    from aiohttp.test_utils import TestClient, TestServer
    from original_client_server import create_original_client_server_runtime
    from runtime.video_reply_settings import VideoReplySettingsStore

    monkeypatch.setattr(local_server, "store", local_server.Store())
    monkeypatch.setattr(local_server, "_state_root", lambda: tmp_path)
    monkeypatch.setattr(
        local_server,
        "video_reply_settings_store",
        VideoReplySettingsStore.initialize(tmp_path),
    )
    monkeypatch.setattr(
        local_server,
        "_conversation_memory_ready_for_reply",
        lambda: True,
    )
    scheduled: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        local_server,
        "_schedule_reply_job",
        lambda *args, **_kwargs: scheduled.append(args),
    )

    async def provider_forbidden(*_args, **_kwargs):
        raise AssertionError("provider must remain deferred")

    monkeypatch.setattr(local_server, "generate_reply", provider_forbidden)

    async def scenario() -> tuple[dict[str, object], dict[str, object]]:
        runtime = create_original_client_server_runtime(
            local_server.handler,
            letter_collection=local_server._letter_collection,
        )
        async with TestClient(TestServer(runtime.app, access_log=None)) as client:
            native = await client.post(
                "/letter/send?request_id=synthetic-runtime-alias",
                json={"content": "synthetic runtime alias letter"},
            )
            toy = await client.post(
                "/toy/letter/send?request_id=synthetic-runtime-alias",
                json={"content": "synthetic runtime alias letter"},
            )
            assert native.status == toy.status == 200
            return await native.json(), await toy.json()

    native_payload, toy_payload = asyncio.run(scenario())

    assert native_payload == toy_payload
    assert native_payload["data"]["letterId"] == native_payload["data"]["letter_id"]
    assert local_server.store.request_keys["synthetic-runtime-alias"] == native_payload[
        "data"
    ]["letter_id"]
    assert len(scheduled) == 1


def test_unknown_native_letter_path_is_not_prefix_mapped() -> None:
    import local_server

    result = asyncio.run(
        local_server.route(
            "POST",
            "/letter/legacy/import",
            {"letters": []},
            {"scope": "legacy"},
        )
    )

    assert result == {
        "code": 501,
        "message": "NOT_IMPLEMENTED",
        "data": {
            "status": "NOT_IMPLEMENTED",
            "error_code": "ROUTE_NOT_IMPLEMENTED",
        },
    }
