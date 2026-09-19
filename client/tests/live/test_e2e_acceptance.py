from __future__ import annotations

import asyncio
import json

from live import LiveService


def test_e2e_harness_reports_a_sanitized_mock_text_turn() -> None:
    from tools.live_e2e_acceptance import run_live_acceptance

    async def exercise() -> dict[str, object]:
        service = LiveService.from_environment(
            environ={"OLIVIA_LLM_PROVIDER": "mock", "OLIVIA_LLM_STREAM": "true"}
        )
        return await run_live_acceptance(service=service, text="private user prompt")

    report = asyncio.run(exercise())

    assert report["status"] == "COMPLETED"
    assert report["network_called"] is False
    assert report["input"] == {"kind": "text"}
    assert report["result"]["text_source"] == "llm"
    assert report["metrics"]["e2e_wall_ms"] >= 0
    assert report["timeline"]
    rendered = json.dumps(report, ensure_ascii=False)
    assert "private user prompt" not in rendered
    assert "system_prompt" not in rendered


def test_e2e_harness_never_starts_a_default_network_turn() -> None:
    from tools.live_e2e_acceptance import run_live_acceptance

    report = asyncio.run(run_live_acceptance(text="do not send this"))

    assert report["status"] == "UNAVAILABLE"
    assert report["network_called"] is False
    assert report["result"]["error_code"] == "NETWORK_NOT_ALLOWED"
    assert "do not send this" not in json.dumps(report, ensure_ascii=False)


def test_e2e_harness_rejects_an_injected_network_gateway_before_the_turn() -> None:
    from llm_gateway import Gateway, GatewayResponse
    from tools.live_e2e_acceptance import run_live_acceptance

    class NetworkGateway(Gateway):
        async def complete(self, messages, *, request_id=None):
            self.mark_network_call()
            return GatewayResponse("must not be reached", request_id or "network", "test", "test")

    gateway = NetworkGateway()
    report = asyncio.run(
        run_live_acceptance(
            service=LiveService(gateway=gateway),
            text="do not send injected text",
        )
    )

    assert report["status"] == "UNAVAILABLE"
    assert report["network_called"] is False
    assert report["result"]["error_code"] == "NETWORK_NOT_ALLOWED"
    assert gateway.network_call_count == 0


def test_e2e_harness_records_native_asr_and_network_truth_without_text(tmp_path) -> None:
    import wave

    from asr.contracts import EventClock
    from llm_gateway import Gateway, GatewayResponse
    from tools.live_e2e_acceptance import run_live_acceptance

    class FakeAsrSession:
        def __init__(self) -> None:
            self.clock = EventClock("e2e-asr")
            self.events_queue: asyncio.Queue = asyncio.Queue()

        async def send_audio(self, pcm16: bytes) -> None:
            assert pcm16

        async def commit(self) -> None:
            await self.events_queue.put(self.clock.emit("final", provider="e2e-asr", text="private transcript"))

        async def events(self):
            yield await self.events_queue.get()

        async def close(self) -> None:
            return None

    class FakeAsr:
        def status(self) -> dict[str, object]:
            return {"status": "available", "ready": True, "is_asr": True, "provider": "e2e-asr"}

        async def open_session(self) -> FakeAsrSession:
            return FakeAsrSession()

    class NetworkGateway(Gateway):
        async def complete(self, messages, *, request_id=None):
            self.mark_network_call()
            return GatewayResponse("private provider reply", request_id or "e2e", "test", "test")

    audio_path = tmp_path / "input.wav"
    with wave.open(str(audio_path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x01\x00" * 1600)

    report = asyncio.run(
        run_live_acceptance(
            service=LiveService(gateway=NetworkGateway(), asr_provider=FakeAsr()),
            audio_path=audio_path,
            allow_network=True,
        )
    )

    assert report["status"] == "COMPLETED"
    assert report["network_called"] is True
    assert report["input"] == {"kind": "audio"}
    assert report["metrics"]["asr_final_at_ms"] is not None
    assert any(item["event"] == "asr_final" for item in report["timeline"])
    rendered = json.dumps(report, ensure_ascii=False)
    assert "private transcript" not in rendered
    assert "private provider reply" not in rendered


def test_e2e_harness_reports_public_cancellation_truthfully() -> None:
    from llm_gateway import Gateway, GatewayResponse
    from tools.live_e2e_acceptance import run_live_acceptance

    class SlowGateway(Gateway):
        acceptance_offline_test_only = True

        async def complete(self, messages, *, request_id=None):
            await asyncio.sleep(0.2)
            return GatewayResponse("should not be reported", request_id or "slow", "test", "test")

    report = asyncio.run(
        run_live_acceptance(
            service=LiveService(gateway=SlowGateway()),
            text="cancel private text",
            cancel_after_ms=0,
        )
    )

    assert report["status"] == "CANCELLED"
    assert report["cancellation"] == {"requested": True, "accepted": True}
    assert report["result"]["error_code"] == "LIVE_CANCELED"
    assert any(item["event"] == "turn_cancelled" for item in report["timeline"])
    assert "cancel private text" not in json.dumps(report, ensure_ascii=False)
