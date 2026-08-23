from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from llm_gateway import (
    GatewayConfig,
    InvalidGatewayInput,
    OfflineDeterministicAdapter,
    OpenAICompatibleAdapter,
    ProviderProtocolError,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
    UnconfiguredAdapter,
    create_gateway,
    validate_messages,
)


ROOT_MESSAGES = (
    {"role": "system", "content": "draft system"},
    {"role": "user", "content": "synthetic letter"},
)


def run(coro):
    return asyncio.run(coro)


def make_config(base_url: str, **overrides) -> GatewayConfig:
    values = {
        "provider": "openai_compatible",
        "base_url": base_url,
        "model": "synthetic-model",
        "api_key_env": "B03_TEST_KEY",
        "api_style": "chat_completions",
        "timeout_seconds": 0.5,
        "max_retries": 0,
        "retry_backoff_seconds": 0,
        "requires_api_key": True,
    }
    values.update(overrides)
    return GatewayConfig(**values)


def test_input_roles_and_lengths_are_rejected_before_provider_call() -> None:
    with pytest.raises(InvalidGatewayInput) as role_error:
        validate_messages(
            [{"role": "tool", "content": "not allowed"}],
            max_input_chars=100,
        )
    assert role_error.value.code == "INVALID_ROLE"

    with pytest.raises(InvalidGatewayInput) as length_error:
        validate_messages(
            [{"role": "user", "content": "12345"}],
            max_input_chars=4,
        )
    assert length_error.value.code == "INPUT_TOO_LONG"


def test_unconfigured_adapter_reports_provider_unavailable() -> None:
    async def exercise():
        with pytest.raises(ProviderUnavailable):
            await UnconfiguredAdapter().complete(ROOT_MESSAGES)

    run(exercise())


def test_offline_adapter_is_deterministic_and_streamable() -> None:
    config = GatewayConfig(provider="mock", model="offline", stream=True)

    async def exercise():
        adapter = OfflineDeterministicAdapter(config)
        first = await adapter.complete(ROOT_MESSAGES, request_id="request-1")
        second = await adapter.complete(ROOT_MESSAGES, request_id="request-2")
        deltas = [delta async for delta in adapter.stream(ROOT_MESSAGES, request_id="request-3")]
        return first, second, deltas

    first, second, deltas = run(exercise())
    assert first.text == second.text
    assert first.request_id == "request-1"
    assert "离线回信" in first.text
    assert "".join(delta.text for delta in deltas) == first.text
    assert deltas[-1].finish_reason == "stop"


def test_explicit_mock_fallback_is_usable_without_network() -> None:
    config = GatewayConfig(
        provider="openai_compatible",
        base_url="https://llm.example.invalid/v1",
        model="synthetic-model",
        requires_api_key=True,
        api_key_env="MISSING_B03_KEY",
        fallback_provider="mock",
    )

    response = run(create_gateway(config).complete(ROOT_MESSAGES))
    assert "离线回信" in response.text


def test_chat_completion_adapter_uses_local_mock_server_and_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    async def exercise():
        seen = {}

        async def handler(request: web.Request) -> web.Response:
            seen["auth"] = request.headers.get("Authorization")
            seen["idempotency_key"] = request.headers.get("Idempotency-Key")
            seen["request_id"] = request.headers.get("X-Request-ID")
            seen["body"] = await request.json()
            return web.json_response(
                {"choices": [{"message": {"role": "assistant", "content": "mock reply"}}]}
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        async with TestClient(TestServer(app)) as client:
            config = make_config(str(client.make_url("/v1")))
            adapter = OpenAICompatibleAdapter(config)
            response = await adapter.complete(ROOT_MESSAGES, request_id="request-1")
        return response, seen

    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    response, seen = run(exercise())
    assert response.text == "mock reply"
    assert response.request_id == "request-1"
    assert seen["auth"] == "Bearer TEST"
    assert seen["idempotency_key"] == "request-1"
    assert seen["request_id"] == "request-1"
    assert seen["body"]["model"] == "synthetic-model"
    assert seen["body"]["messages"] == list(ROOT_MESSAGES)


def test_responses_style_and_sse_stream_are_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    async def exercise():
        async def response_handler(_request: web.Request) -> web.Response:
            return web.json_response({"output_text": "responses reply"})

        async def stream_handler(_request: web.Request) -> web.StreamResponse:
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(_request)
            await response.write(b'data: {"type":"response.output_text.delta","delta":"first "}\n\n')
            await response.write(b'data: {"type":"response.output_text.delta","delta":"second"}\n\n')
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_post("/v1/responses", response_handler)
        app.router.add_post("/v1/chat/completions", stream_handler)
        async with TestClient(TestServer(app)) as client:
            responses_config = make_config(
                str(client.make_url("/v1")),
                api_style="responses",
            )
            response = await OpenAICompatibleAdapter(responses_config).complete(ROOT_MESSAGES)
            stream_config = make_config(str(client.make_url("/v1")), stream=True)
            stream = [delta async for delta in OpenAICompatibleAdapter(stream_config).stream(ROOT_MESSAGES)]
            return response, stream

    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    response, stream = run(exercise())
    assert response.text == "responses reply"
    assert "".join(delta.text for delta in stream) == "first second"


@pytest.mark.parametrize("status", [429, 503])
def test_retryable_http_status_retries_before_success(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    async def exercise():
        calls = 0

        async def handler(_request: web.Request) -> web.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return web.Response(status=status, text="provider body must stay private")
            return web.json_response({"choices": [{"message": {"content": "recovered"}}]})

        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        async with TestClient(TestServer(app)) as client:
            config = make_config(str(client.make_url("/v1")), max_retries=1)
            result = await OpenAICompatibleAdapter(config).complete(ROOT_MESSAGES)
        return calls, result

    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    calls, result = run(exercise())
    assert calls == 2
    assert result.text == "recovered"


def test_non_retryable_4xx_bad_json_and_empty_response_are_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    async def exercise():
        async def rejected(_request: web.Request) -> web.Response:
            return web.Response(status=400, text="private provider details")

        async def bad_json(_request: web.Request) -> web.Response:
            return web.Response(status=200, text="not json")

        async def empty(_request: web.Request) -> web.Response:
            return web.json_response({})

        app = web.Application()
        app.router.add_post("/v1/rejected/chat/completions", rejected)
        app.router.add_post("/v1/bad/chat/completions", bad_json)
        app.router.add_post("/v1/empty/chat/completions", empty)
        async with TestClient(TestServer(app)) as client:
            rejected_adapter = OpenAICompatibleAdapter(make_config(str(client.make_url("/v1/rejected"))))
            bad_adapter = OpenAICompatibleAdapter(make_config(str(client.make_url("/v1/bad"))))
            empty_adapter = OpenAICompatibleAdapter(make_config(str(client.make_url("/v1/empty"))))
            with pytest.raises(ProviderRejected) as rejected_error:
                await rejected_adapter.complete(ROOT_MESSAGES)
            with pytest.raises(ProviderProtocolError):
                await bad_adapter.complete(ROOT_MESSAGES)
            with pytest.raises(ProviderProtocolError):
                await empty_adapter.complete(ROOT_MESSAGES)
        return rejected_error.value

    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    rejected_error = run(exercise())
    assert rejected_error.status == 400


def test_timeout_is_explicit_and_does_not_expose_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    async def exercise():
        async def handler(_request: web.Request) -> web.Response:
            await asyncio.sleep(0.2)
            return web.json_response({"choices": [{"message": {"content": "late"}}]})

        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        async with TestClient(TestServer(app)) as client:
            adapter = OpenAICompatibleAdapter(
                make_config(str(client.make_url("/v1")), timeout_seconds=0.05)
            )
            with pytest.raises(ProviderTimeout) as error:
                await adapter.complete(ROOT_MESSAGES)
        return error.value

    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    error = run(exercise())
    assert str(error) == "PROVIDER_TIMEOUT"
    assert "late" not in str(error)
