from __future__ import annotations

from pathlib import Path

from live import LiveConfig, LiveService
from llm_gateway import GatewayResponse
from memory_port import NullMemoryPort


ROOT = Path(__file__).resolve().parents[2]


class _Closable:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _RecordingGateway:
    stream_enabled = False

    def __init__(self, config) -> None:
        self.config = config
        self.calls = []

    async def complete(self, messages, *, request_id=None):
        self.calls.append(tuple(messages))
        return GatewayResponse(
            "synthetic live reply",
            request_id or "request",
            "mock",
            "mock",
        )


def _capture_live_gateways(monkeypatch):
    import live.environment as environment

    gateways = []

    def create_recording_gateway(config):
        gateway = _RecordingGateway(config)
        gateways.append(gateway)
        return gateway

    monkeypatch.setattr(environment, "create_gateway", create_recording_gateway)
    return gateways


def test_live_default_loads_ready_persona_v2_for_future_im(monkeypatch) -> None:
    import asyncio

    gateways = _capture_live_gateways(monkeypatch)

    async def exercise():
        service = LiveService.from_environment(
            environ={"OLIVIA_LLM_PROVIDER": "mock"},
            project_root=str(ROOT),
        )
        session = await service.start_session("persona-v2-user")
        first = await session.send_text("synthetic first message")
        second = await session.send_text("synthetic current message")
        await service.stop()
        return first, second

    results = asyncio.run(exercise())

    assert [result.status for result in results] == ["completed", "completed"]
    assert len(gateways) == 1
    assert len(gateways[0].calls) == 2
    for messages in gateways[0].calls:
        assert [message["role"] for message in messages] == ["system", "user"]
        assert '"mode":"future_im"' in messages[0]["content"]
        assert "Persona status is DRAFT" not in messages[0]["content"]
    assert gateways[0].calls[0][0]["content"] != gateways[0].calls[1][0]["content"]
    second_system = gateways[0].calls[1][0]["content"]
    assert "user_message: synthetic first message" in second_system
    assert "character_reply: synthetic live reply" in second_system
    assert gateways[0].calls[1][1]["content"] == "synthetic current message"


def test_live_persona_load_and_budget_failures_stop_before_provider_call(
    tmp_path, monkeypatch
) -> None:
    import asyncio

    gateways = _capture_live_gateways(monkeypatch)

    async def exercise():
        missing_service = LiveService.from_environment(
            environ={
                "OLIVIA_LLM_PROVIDER": "mock",
                "OLIVIA_PERSONA_V2_FILE": "missing-persona.json",
            },
            project_root=str(tmp_path),
        )
        missing_session = await missing_service.start_session("missing-persona-user")
        try:
            missing_result = await asyncio.wait_for(
                missing_session.send_text("synthetic current message"),
                timeout=0.5,
            )
        finally:
            await missing_service.stop()

        budget_service = LiveService.from_environment(
            environ={
                "OLIVIA_LLM_PROVIDER": "mock",
                "OLIVIA_LLM_MAX_INPUT_CHARS": "100",
            },
            project_root=str(ROOT),
        )
        budget_session = await budget_service.start_session("budget-user")
        try:
            budget_result = await asyncio.wait_for(
                budget_session.send_text("synthetic current message"),
                timeout=0.5,
            )
        finally:
            await budget_service.stop()
        return missing_result, budget_result

    results = asyncio.run(exercise())

    assert [result.status for result in results] == ["failed", "failed"]
    assert [result.error_code for result in results] == [
        "LIVE_LLM_ERROR",
        "LIVE_LLM_ERROR",
    ]
    assert len(gateways) == 2
    assert all(gateway.calls == [] for gateway in gateways)


def test_live_environment_uses_the_stricter_gateway_input_limit() -> None:
    service = LiveService.from_environment(
        environ={
            "OLIVIA_LLM_PROVIDER": "mock",
            "OLIVIA_LLM_MAX_INPUT_CHARS": "3200",
        },
        project_root=str(ROOT),
        config=LiveConfig(max_input_chars=4800),
    )

    assert service.config.max_input_chars == 3200


def test_live_legacy_persona_requires_explicit_v2_opt_out() -> None:
    service = LiveService.from_environment(
        environ={
            "OLIVIA_LLM_PROVIDER": "mock",
            "OLIVIA_PERSONA_V2_ENABLED": "false",
        },
        project_root=str(ROOT),
    )

    snapshot = service.persona_provider.snapshot()

    assert snapshot.status == "DRAFT"
    assert snapshot.system_prompt.startswith("PERSONA STATUS: DRAFT")


def test_explicit_memory_port_bypasses_project_memory_configuration(
    tmp_path, monkeypatch
) -> None:
    import live.environment as environment

    injected = NullMemoryPort()

    def fail(*_args, **_kwargs):
        raise AssertionError("project memory configuration must not be read")

    monkeypatch.setattr(environment, "load_memory_config", fail)

    service = LiveService.from_environment(
        environ={"OLIVIA_LLM_PROVIDER": "mock"},
        project_root=str(tmp_path),
        memory_port=injected,
    )

    assert service.memory_port is injected
    assert service.environment.memory_port is injected


def test_environment_composes_existing_provider_factories_and_never_promotes_unready_components() -> None:
    service = LiveService.from_environment(
        environ={
            "OLIVIA_LLM_PROVIDER": "mock",
            "ASR_PROVIDER": "nemotron-speech-cpp",
            "ASR_RUNTIME_ROOT": "D:/missing-b08-runtime",
            "ASR_MODEL_ROOT": "D:/missing-b08-models",
            "ASR_CACHE_ROOT": "D:/missing-b08-cache",
            "OLIVIA_TTS_PROVIDER": "cosyvoice3",
            "OLIVIA_TTS_ENABLED": "true",
            "OLIVIA_TTS_RUNTIME_ROOT": "D:/missing-b08-tts-runtime",
            "OLIVIA_TTS_MODEL_DIR": "D:/missing-b08-tts-model",
            "OLIVIA_TTS_REFERENCE_AUDIO": "D:/missing-b08-reference.wav",
        }
    )

    health = service.health()

    assert health["components"]["llm"]["status"] == "READY"
    assert health["components"]["llm"]["provider"] == "mock"
    assert health["components"]["asr"]["ready"] is False
    assert health["components"]["asr"]["reason_code"] == "ASR_RUNTIME_MISSING"
    assert health["components"]["tts"]["ready"] is False
    assert health["components"]["visual"]["ready"] is False
    assert health["ready"] is False
    assert health["network_called"] is False

    public = service.environment.public_dict()
    assert public["network_called"] is False
    assert public["components"]["memory"]["ready"] is False
    assert public["components"]["asr"]["ready"] is False
    assert public["components"]["tts"]["ready"] is False
    assert "OLIVIA_LLM_PROVIDER" not in repr(public)
    assert all("D:/" not in repr(value) for value in public.values())


def test_environment_mock_llm_runs_through_the_live_public_boundary() -> None:
    import asyncio

    async def exercise():
        service = LiveService.from_environment(
            environ={"OLIVIA_LLM_PROVIDER": "mock", "OLIVIA_LLM_STREAM": "true"}
        )
        session = await service.start_session("user-a")
        result = await session.send_text("assembled provider")
        await service.stop()
        return result

    result = asyncio.run(exercise())

    assert result.status == "completed"
    assert result.text_source == "llm"
    assert result.text


def test_external_llm_configuration_without_key_is_not_reported_ready() -> None:
    service = LiveService.from_environment(
        environ={
            "OLIVIA_LLM_PROVIDER": "openai_compatible",
            "OLIVIA_LLM_BASE_URL": "http://127.0.0.1:9/v1",
            "OLIVIA_LLM_MODEL": "local-model",
            "OLIVIA_LLM_API_KEY_ENV": "B08_MISSING_KEY",
            "OLIVIA_LLM_REQUIRES_API_KEY": "true",
        }
    )

    llm = service.health()["components"]["llm"]

    assert llm["ready"] is False
    assert llm["status"] == "UNAVAILABLE"
    assert llm["reason_code"] == "LLM_API_KEY_UNAVAILABLE"


def test_live_voice_entry_is_truthful_without_deepseek_key(tmp_path) -> None:
    import asyncio
    import json
    import wave

    from tools.live_healthcheck import run_voice_turn

    input_wav = tmp_path / "input.wav"
    with wave.open(str(input_wav), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 1600)

    report = asyncio.run(
        run_voice_turn(
            environ={
                "ASR_PROVIDER": "text-fallback",
                "OLIVIA_TTS_ENABLED": "false",
            },
            audio_path=input_wav,
            output_path=tmp_path / "reply.wav",
        )
    )

    assert report["status"] == "UNAVAILABLE"
    assert report["health"]["components"]["llm"]["reason_code"] == "LLM_API_KEY_UNAVAILABLE"
    assert report["network_called"] is False
    assert report["output_wav"] is None
    payload = json.dumps(report, ensure_ascii=False)
    assert '"text":' not in payload
    assert "provider_response" not in payload


def test_live_voice_entry_uses_existing_tts_output_boundary(tmp_path) -> None:
    import asyncio
    import wave

    from llm_gateway import GatewayResponse
    from tts import TTSConfig, TTSService
    from tts.contracts import AudioChunk
    from tts.registry import TTSProviderRegistry
    from tools.live_healthcheck import run_voice_turn

    class ReplyGateway:
        stream_enabled = False

        async def complete(self, messages, *, request_id=None):
            return GatewayResponse("local voice reply", request_id or "request", "mock", "mock")

    class FakeTts:
        name = "fake"
        license_id = "MIT"

        def __init__(self, _config):
            pass

        def health(self):
            return {"status": "available", "provider": self.name, "license_id": self.license_id}

        def stream_sentence(self, _text, _request, sentence_index):
            yield AudioChunk((0.1, -0.1, 0.1, -0.1), 16000, sentence_index, 0)

        def close(self):
            return None

    registry = TTSProviderRegistry()
    registry.register("fake", FakeTts, license_id="MIT")
    service = LiveService(
        gateway=ReplyGateway(),
        tts_service=TTSService(TTSConfig(enabled=True, provider="fake"), registry=registry),
    )

    output_wav = tmp_path / "reply.wav"
    report = asyncio.run(
        run_voice_turn(
            service=service,
            text="hello",
            output_path=output_wav,
        )
    )

    assert report["status"] == "COMPLETED"
    assert report["result"]["audio_chunks"] == 1
    assert report["output_wav"] == str(output_wav.absolute())
    with wave.open(str(output_wav), "rb") as audio:
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.getframerate() == 16000
        assert audio.getnframes() == 4


def test_live_voice_entry_streams_native_asr_into_live_and_redacts_provider_text(tmp_path) -> None:
    import asyncio
    import json
    import wave

    from asr.contracts import EventClock
    from llm_gateway import Gateway, GatewayResponse
    from tools.live_healthcheck import run_voice_turn

    class FakeAsrSession:
        def __init__(self):
            self.clock = EventClock("fake-native")
            self.final = asyncio.Queue()

        async def send_audio(self, pcm16):
            assert pcm16

        async def commit(self):
            await self.final.put(self.clock.emit("final", provider="fake-native", text="hello from mic"))

        async def events(self):
            yield await self.final.get()

        async def close(self):
            return None

    class FakeAsr:
        def status(self):
            return {"status": "available", "ready": True, "is_asr": True, "provider": "fake-native"}

        async def open_session(self):
            return FakeAsrSession()

    class ReplyGateway(Gateway):
        async def complete(self, messages, *, request_id=None):
            return GatewayResponse("provider reply body", request_id or "request", "mock", "mock")

    input_wav = tmp_path / "mic.wav"
    with wave.open(str(input_wav), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x01\x00" * 3200)

    report = asyncio.run(
        run_voice_turn(
            service=LiveService(gateway=ReplyGateway(), asr_provider=FakeAsr()),
            audio_path=input_wav,
        )
    )

    assert report["status"] == "COMPLETED"
    assert report["transcript"] == "hello from mic"
    assert any(item["event"] == "asr_final" for item in report["timestamps"])
    assert report["result"]["assistant_text_present"] is True
    assert all("provider reply body" not in json.dumps(item, ensure_ascii=False) for item in report["timestamps"])


def test_live_voice_network_called_uses_gateway_observation_not_text_presence() -> None:
    import asyncio

    from llm_gateway import Gateway, GatewayConfig, GatewayResponse, create_gateway
    from tools.live_healthcheck import run_voice_turn

    class OfflineTextGateway(Gateway):
        async def complete(self, messages, *, request_id=None):
            return GatewayResponse("offline reply", request_id or "offline", "offline", "offline")

    class TracedNetworkGateway(Gateway):
        async def complete(self, messages, *, request_id=None):
            self.mark_network_call()
            return GatewayResponse("provider reply", request_id or "network", "provider", "model")

    offline_report = asyncio.run(
        run_voice_turn(service=LiveService(gateway=OfflineTextGateway()), text="offline")
    )
    mock_report = asyncio.run(
        run_voice_turn(
            service=LiveService(gateway=create_gateway(GatewayConfig(provider="mock", model="offline"))),
            text="mock",
        )
    )
    network_report = asyncio.run(
        run_voice_turn(service=LiveService(gateway=TracedNetworkGateway()), text="network")
    )

    assert offline_report["result"]["assistant_text_present"] is True
    assert offline_report["network_called"] is False
    assert mock_report["result"]["assistant_text_present"] is True
    assert mock_report["network_called"] is False
    assert network_report["result"]["assistant_text_present"] is True
    assert network_report["network_called"] is True


def test_live_voice_network_called_after_openai_compatible_http_attempt(monkeypatch) -> None:
    """The public report follows adapter I/O evidence, not generated text."""
    import asyncio
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from llm_gateway import GatewayConfig, OpenAICompatibleAdapter
    from tools.live_healthcheck import run_voice_turn

    received: list[dict[str, object]] = []

    class LocalOpenAIHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received.append({"path": self.path, "body": json.loads(self.rfile.read(length))})
            payload = json.dumps({"choices": [{"message": {"content": "local-only response"}}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalOpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("B11_LOCAL_HTTP_KEY", "test-only-local-key")
    try:
        adapter = OpenAICompatibleAdapter(
            GatewayConfig(
                provider="openai_compatible",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                model="local-test-model",
                api_key_env="B11_LOCAL_HTTP_KEY",
                requires_api_key=True,
                timeout_seconds=2,
            )
        )
        report = asyncio.run(run_voice_turn(service=LiveService(gateway=adapter), text="local HTTP turn"))
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert received and received[0]["path"] == "/v1/chat/completions"
    assert report["result"]["assistant_text_present"] is True
    assert report["network_called"] is True


def test_supplied_environment_key_is_presence_only_and_reachability_stays_unverified() -> None:
    service = LiveService.from_environment(
        environ={
            "OLIVIA_LLM_PROVIDER": "openai_compatible",
            "OLIVIA_LLM_BASE_URL": "http://127.0.0.1:9/v1",
            "OLIVIA_LLM_MODEL": "local-model",
            "OLIVIA_LLM_API_KEY_ENV": "B08_SUPPLIED_KEY",
            "OLIVIA_LLM_REQUIRES_API_KEY": "true",
            "B08_SUPPLIED_KEY": "synthetic-secret",
        }
    )

    health = service.health()
    llm = health["components"]["llm"]

    assert llm["status"] == "DEGRADED"
    assert llm["ready"] is False
    assert llm["reason_code"] == "LLM_REACHABILITY_UNVERIFIED"
    public = service.environment.public_dict()
    assert public["components"]["llm"]["api_key_configured"] is True
    assert "synthetic-secret" not in repr(public)


def test_composition_factory_errors_fail_closed_per_component(monkeypatch) -> None:
    import live.environment as environment

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic factory failure")

    monkeypatch.setattr(environment, "create_gateway", fail)
    monkeypatch.setattr(environment, "create_memory_adapter", fail)
    monkeypatch.setattr(environment, "create_provider", fail)
    monkeypatch.setattr(environment, "_load_tts_config", fail)
    monkeypatch.setattr(environment, "TTSService", fail)
    monkeypatch.setattr(environment, "ConfigPersonaProvider", fail)
    monkeypatch.setattr(environment, "load_persona", fail)

    service = LiveService.from_environment(
        environ={
            "OLIVIA_LLM_PROVIDER": "mock",
            "OLIVIA_MEMORY_ENABLED": "true",
            "ASR_PROVIDER": "nemotron-speech-cpp",
            "OLIVIA_TTS_ENABLED": "true",
        }
    )

    health = service.health()

    assert health["components"]["llm"]["ready"] is False
    assert health["components"]["memory"]["ready"] is False
    assert health["components"]["asr"]["ready"] is False
    assert health["components"]["tts"]["ready"] is False
    assert service.environment.construction_errors == {
        "llm": "LLM_UNAVAILABLE",
        "memory": "MEMORY_UNAVAILABLE",
        "asr": "ASR_CONFIG_INVALID",
        "tts": "TTS_CONFIG_INVALID",
        "persona": "PERSONA_UNAVAILABLE",
    }


def test_service_stop_closes_composed_resources_once() -> None:
    memory = _Closable()
    tts = _Closable()
    service = LiveService(memory_port=memory, tts_service=tts)

    import asyncio

    asyncio.run(service.stop())
    asyncio.run(service.stop())

    assert memory.close_calls == 1
    assert tts.close_calls == 1


def test_service_stop_waits_for_an_active_turn_to_finish_cancellation() -> None:
    import asyncio

    from llm_gateway import Gateway, GatewayResponse

    class SlowGateway(Gateway):
        async def complete(self, messages, *, request_id=None):
            await asyncio.sleep(10)
            return GatewayResponse("late", request_id or "request", "slow", "model")

    async def exercise():
        service = LiveService(gateway=SlowGateway())
        session = await service.start_session("user-a")
        handle = await session.submit_text("stop me")
        await asyncio.sleep(0)
        await service.stop()
        return await asyncio.wait_for(handle.wait(), timeout=0.2)

    result = asyncio.run(exercise())

    assert result.status == "cancelled"
    assert result.error_code == "LIVE_CANCELED"
