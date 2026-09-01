from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from llm_gateway import (
    GatewayConfig,
    GatewayRequestScope,
    InvalidGatewayInput,
    ManagedLLMConfig,
    OfflineDeterministicAdapter,
    OpenAICompatibleAdapter,
    ProviderProtocolError,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
    UnconfiguredAdapter,
    create_gateway,
    load_gateway_config,
    validate_messages,
)


def test_bound_gateway_key_does_not_follow_later_environment_changes(
    monkeypatch,
) -> None:
    config = GatewayConfig(
        provider="openai_compatible",
        base_url="https://old.example/v1",
        model="old-model",
        api_key_env="SHARED_KEY",
        requires_api_key=True,
    )
    monkeypatch.setenv("SHARED_KEY", "new-key")
    adapter = OpenAICompatibleAdapter(
        config,
        key_resolver=lambda: "old-key",
    )

    assert adapter._key() == "old-key"


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


def test_reasoning_timeout_is_an_explicit_bounded_public_config() -> None:
    default = GatewayConfig.from_mapping({})
    configured = GatewayConfig.from_mapping({"reasoning_timeout_seconds": 720})
    bounded = GatewayConfig.from_mapping({"reasoning_timeout_seconds": 99_999})

    assert default.reasoning_timeout_seconds == 600.0
    assert configured.reasoning_timeout_seconds == 720.0
    assert bounded.reasoning_timeout_seconds == 1800.0
    assert configured.public_dict()["reasoning_timeout_seconds"] == 720.0


def test_retry_backoff_default_matches_public_template() -> None:
    assert GatewayConfig().retry_backoff_seconds == 0.25
    assert GatewayConfig.from_mapping({}).retry_backoff_seconds == 0.25


def test_managed_llm_config_uses_one_strict_schema_for_runtime_and_restart() -> None:
    config = ManagedLLMConfig.from_mapping(
        {
            "schema_version": 3,
            "provider": "openai_compatible",
            "base_url": "https://gateway.example/v1/",
            "model": "vendor/not-deepseek",
            "max_retries": 4,
        }
    )

    assert config == ManagedLLMConfig(
        provider="openai_compatible",
        base_url="https://gateway.example/v1",
        model="vendor/not-deepseek",
        max_retries=4,
    )
    assert config.to_mapping() == {
        "schema_version": 3,
        "provider": "openai_compatible",
        "base_url": "https://gateway.example/v1",
        "model": "vendor/not-deepseek",
        "max_retries": 4,
    }


@pytest.mark.parametrize("missing", ["provider", "max_retries"])
def test_managed_llm_schema_v3_requires_provider_and_retry_count(missing: str) -> None:
    payload = {
        "schema_version": 3,
        "provider": "openai_compatible",
        "base_url": "https://gateway.example/v1",
        "model": "vendor/not-deepseek",
        "max_retries": 2,
    }
    payload.pop(missing)

    with pytest.raises(ValueError, match="managed LLM"):
        ManagedLLMConfig.from_mapping(payload)


def test_reasoning_timeout_environment_override_is_loaded(tmp_path) -> None:
    config = load_gateway_config(
        tmp_path / "missing.json",
        environ={"OLIVIA_LLM_REASONING_TIMEOUT_SECONDS": "840"},
    )

    assert config.reasoning_timeout_seconds == 840.0


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
            seen["user_agent"] = request.headers.get("User-Agent")
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
    assert seen["user_agent"] == "Olivia-Community/0.1"
    assert seen["idempotency_key"] is None
    assert seen["request_id"] == "request-1"
    assert seen["body"]["model"] == "synthetic-model"
    assert seen["body"]["messages"] == list(ROOT_MESSAGES)


def test_nonstream_length_finish_reason_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> ProviderProtocolError:
        async def handler(_request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {"content": "partial private reply"},
                            "finish_reason": "length",
                        }
                    ]
                }
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        async with TestClient(TestServer(app)) as client:
            adapter = OpenAICompatibleAdapter(make_config(str(client.make_url("/v1"))))
            with pytest.raises(ProviderProtocolError) as caught:
                await adapter.complete(ROOT_MESSAGES)
        return caught.value

    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    error = run(exercise())

    assert str(error) == "PROVIDER_PROTOCOL"
    assert "partial private reply" not in str(error)


@pytest.mark.parametrize("endpoint", ["opencode-go", "official-deepseek"])
@pytest.mark.parametrize("request_id", ["letter-reply:fixture", "quality-fixture"])
def test_deepseek_v4_flash_release_text_requests_max_reasoning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    endpoint: str,
    request_id: str,
) -> None:
    async def exercise():
        seen: dict[str, object] = {}

        async def handler(request: web.Request) -> web.Response:
            seen["body"] = await request.json()
            seen["request_id"] = request.headers.get("X-Request-ID")
            seen["idempotency_key"] = request.headers.get("Idempotency-Key")
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "reasoning_content": "private chain",
                                "content": "mock reply",
                            }
                        }
                    ]
                }
            )

        app = web.Application()
        app.router.add_post(f"/{endpoint}/v1/chat/completions", handler)
        async with TestClient(TestServer(app)) as client:
            adapter = OpenAICompatibleAdapter(
                make_config(
                    str(client.make_url(f"/{endpoint}/v1")),
                    model="DeepSeek-V4-Flash",
                )
            )
            response = await adapter.complete_scoped(
                ROOT_MESSAGES,
                request_id=request_id,
                scope=GatewayRequestScope.TEXT_LETTER_MAX_REASONING,
            )
        return response, seen

    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    response, seen = run(exercise())

    assert response.text == "mock reply"
    assert response.request_id == request_id
    body = seen["body"]
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "max"
    assert seen["request_id"] == request_id
    assert seen["idempotency_key"] == (
        request_id if request_id.startswith("letter-reply:") else None
    )
    captured = capsys.readouterr()
    assert "private chain" not in captured.out
    assert "private chain" not in captured.err


@pytest.mark.parametrize(
    "request_id",
    [None, "letter-reply:video", "quality-video", "historical-import", "live-turn", "song-plan"],
)
def test_deepseek_v4_flash_nonrelease_text_requests_keep_legacy_payload(
    monkeypatch: pytest.MonkeyPatch,
    request_id: str | None,
) -> None:
    async def exercise() -> dict[str, object]:
        seen: dict[str, object] = {}

        async def handler(request: web.Request) -> web.Response:
            seen.update(await request.json())
            return web.json_response(
                {"choices": [{"message": {"content": "mock reply"}}]}
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        async with TestClient(TestServer(app)) as client:
            adapter = OpenAICompatibleAdapter(
                make_config(
                    str(client.make_url("/v1")),
                    model="deepseek-v4-flash",
                )
            )
            await adapter.complete(ROOT_MESSAGES, request_id=request_id)
        return seen

    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    body = run(exercise())

    assert "thinking" not in body
    assert "reasoning_effort" not in body


def test_deepseek_v4_flash_release_reasoning_has_a_compatible_http_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> tuple[str, str]:
        async def handler(_request: web.Request) -> web.Response:
            await asyncio.sleep(0.05)
            return web.json_response(
                {"choices": [{"message": {"content": "mock reply"}}]}
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        async with TestClient(TestServer(app)) as client:
            adapter = OpenAICompatibleAdapter(
                make_config(
                    str(client.make_url("/v1")),
                    model="deepseek-v4-flash",
                    timeout_seconds=0.01,
                )
            )
            release = await adapter.complete_scoped(
                ROOT_MESSAGES,
                request_id="letter-reply:fixture",
                scope=GatewayRequestScope.TEXT_LETTER_MAX_REASONING,
            )
            with pytest.raises(ProviderTimeout) as nonrelease:
                await adapter.complete(ROOT_MESSAGES, request_id="live-turn")
        return release.text, nonrelease.value.code

    monkeypatch.setenv("B03_TEST_KEY", "TEST")

    assert run(exercise()) == ("mock reply", "PROVIDER_TIMEOUT")


def test_openai_compatible_adapter_returns_required_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise():
        seen = {}

        async def handler(request: web.Request) -> web.Response:
            seen["request_id"] = request.headers.get("X-Request-ID")
            seen["body"] = await request.json()
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "name": "apply_voice_performance",
                                            "arguments": '{"overall_emotion":"steady reassurance"}',
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        async with TestClient(TestServer(app)) as client:
            adapter = OpenAICompatibleAdapter(make_config(str(client.make_url("/v1"))))
            calls = await adapter.complete_with_tools(
                messages=ROOT_MESSAGES,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "apply_voice_performance",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
                tool_choice="required",
                request_id="voice-direction:fixture",
            )
        return calls, seen

    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    calls, seen = run(exercise())

    assert [(call.name, call.arguments) for call in calls] == [
        ("apply_voice_performance", {"overall_emotion": "steady reassurance"})
    ]
    assert seen["request_id"] == "voice-direction:fixture"
    assert seen["body"]["tool_choice"] == "required"
    assert seen["body"]["tools"][0]["function"]["name"] == "apply_voice_performance"


def test_openai_compatible_adapter_rejects_malformed_tool_call_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        async def handler(_request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    42,
                                    {
                                        "function": {
                                            "name": "apply_voice_performance",
                                            "arguments": '{"overall_emotion":"steady reassurance"}',
                                        }
                                    },
                                ]
                            }
                        }
                    ]
                }
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        async with TestClient(TestServer(app)) as client:
            adapter = OpenAICompatibleAdapter(make_config(str(client.make_url("/v1"))))
            with pytest.raises(ProviderProtocolError):
                await adapter.complete_with_tools(
                    messages=ROOT_MESSAGES,
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "apply_voice_performance",
                                "parameters": {"type": "object"},
                            },
                        }
                    ],
                    tool_choice="required",
                )

    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    run(exercise())


def test_openai_compatible_adapter_rejects_non_mapping_function_without_top_level_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        async def handler(_request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": 42,
                                        "name": "apply_voice_performance",
                                        "arguments": '{"overall_emotion":"steady reassurance"}',
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        async with TestClient(TestServer(app)) as client:
            adapter = OpenAICompatibleAdapter(make_config(str(client.make_url("/v1"))))
            with pytest.raises(ProviderProtocolError):
                await adapter.complete_with_tools(
                    messages=ROOT_MESSAGES,
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "apply_voice_performance",
                                "parameters": {"type": "object"},
                            },
                        }
                    ],
                    tool_choice="required",
                )

    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    run(exercise())


def test_tool_completion_rejects_non_required_choice_before_provider_call() -> None:
    async def exercise() -> InvalidGatewayInput:
        adapter = OpenAICompatibleAdapter(
            GatewayConfig(provider="openai_compatible", base_url="", model="")
        )
        with pytest.raises(InvalidGatewayInput) as error:
            await adapter.complete_with_tools(
                messages=ROOT_MESSAGES,
                tools=[{"type": "function", "function": {"name": "apply_voice_performance"}}],
                tool_choice="auto",
            )
        return error.value

    assert run(exercise()).code == "REQUIRED_TOOL_CHOICE"


def test_provider_idempotency_header_is_reserved_for_durable_letter_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> list[str | None]:
        seen: list[str | None] = []

        async def handler(request: web.Request) -> web.Response:
            seen.append(request.headers.get("Idempotency-Key"))
            return web.json_response(
                {"choices": [{"message": {"content": "mock reply"}}]}
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        async with TestClient(TestServer(app)) as client:
            adapter = OpenAICompatibleAdapter(make_config(str(client.make_url("/v1"))))
            await adapter.complete(ROOT_MESSAGES, request_id="letter-reply-mode-router")
            await adapter.complete(
                ROOT_MESSAGES,
                request_id="letter-reply:synthetic-letter-id",
            )
            await adapter.complete(
                ROOT_MESSAGES,
                request_id="letter-reply:synthetic-letter-id:voice-direction",
            )
        return seen

    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    assert run(exercise()) == [
        None,
        "letter-reply:synthetic-letter-id",
        "letter-reply:synthetic-letter-id:voice-direction",
    ]


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


def test_deepseek_v4_flash_release_stream_hides_reasoning_content(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def exercise():
        seen: list[dict[str, object]] = []

        async def handler(request: web.Request) -> web.StreamResponse:
            seen.append(await request.json())
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(
                b'data: {"choices":[{"delta":{"reasoning_content":"private stream chain"}}]}\n\n'
            )
            await response.write(
                b'data: {"choices":[{"delta":{"content":"visible reply"}}]}\n\n'
            )
            await response.write(
                b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            )
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        async with TestClient(TestServer(app)) as client:
            adapter = OpenAICompatibleAdapter(
                make_config(
                    str(client.make_url("/v1")),
                    model="deepseek-v4-flash",
                    stream=True,
                )
            )
            release_deltas = [
                delta
                async for delta in adapter.stream_scoped(
                    ROOT_MESSAGES,
                    request_id="quality-fixture",
                    scope=GatewayRequestScope.TEXT_LETTER_MAX_REASONING,
                )
            ]
            live_deltas = [
                delta
                async for delta in adapter.stream(
                    ROOT_MESSAGES,
                    request_id="live-turn",
                )
            ]
        return release_deltas, live_deltas, seen

    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    release_deltas, live_deltas, bodies = run(exercise())

    assert "".join(delta.text for delta in release_deltas) == "visible reply"
    assert "".join(delta.text for delta in live_deltas) == "visible reply"
    assert bodies[0]["thinking"] == {"type": "enabled"}
    assert bodies[0]["reasoning_effort"] == "max"
    assert "thinking" not in bodies[1]
    assert "reasoning_effort" not in bodies[1]
    captured = capsys.readouterr()
    assert "private stream chain" not in captured.out
    assert "private stream chain" not in captured.err


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
