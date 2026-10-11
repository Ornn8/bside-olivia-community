import asyncio
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import wave

import pytest


def test_tts_ignores_legacy_content_gate_and_only_generates_once(tmp_path, monkeypatch):
    from tts import delivery
    calls = []
    monkeypatch.setattr(delivery, 'build_external_delivery_request', lambda *a: {'max_attempts': 3, 'quality_gate_required': True, 'text': '测试', 'seed': 1})
    def configured(config, **kwargs):
        assert kwargs['require_quality_gate'] is False
        return True
    monkeypatch.setattr(delivery, 'delivery_configured', configured)
    def worker(command, **kwargs):
        calls.append(command)
        with wave.open(command[command.index('--output') + 1], 'wb') as audio:
            audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\x01\x00' * 24000)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(delivery, '_run_breeze_worker', worker)
    monkeypatch.setattr(delivery.subprocess, 'run', lambda *a, **k: pytest.fail('Content reviewer must not run'))
    config = SimpleNamespace(provider='breeze_tts2', provider_options={'external_python': sys.executable})
    plan = SimpleNamespace(spoken_text='测试', cues=(1,), speech_units=lambda: (1,))
    result = delivery.render_delivery_wav(config, plan, tmp_path / 'out.wav', enforce_content_gate=True)
    assert len(calls) == 1
    assert result.quality_report is None


def test_invalid_lyrics_do_not_trigger_another_generation():
    from llm_gateway import GatewayConfig
    from runtime.media.song_content import plan_song_content
    class Gateway:
        config = GatewayConfig(persona_v2_enabled=False)
        calls = 0
        async def complete(self, messages):
            self.calls += 1
            return SimpleNamespace(text=json.dumps({'verse': ['短句'], 'chorus': ['短句']}))
    gateway = Gateway()
    with pytest.raises(ValueError):
        plan_song_content('测试', '测试', 110, gateway=gateway)
    assert gateway.calls == 1


def test_gateway_creation_preserves_configured_llm_retry():
    from llm_gateway import create_gateway, GatewayConfig
    gateway = create_gateway(GatewayConfig(provider='openai_compatible', feature_enabled=True, max_retries=4, fallback_provider='none'))
    assert gateway.config.max_retries == 4
