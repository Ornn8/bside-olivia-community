import hashlib
import json
from pathlib import Path

import pytest

from tts.breeze_adapter import adapter_metadata


def test_adapter_metadata_validates_digest_and_keeps_paths_private(tmp_path):
    artifact = tmp_path / 'adapter.safetensors'
    artifact.write_bytes(b'synthetic adapter')
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    manifest = {'schema_version': 1, 'artifact_type': 'breeze_lora_adapter',
                'base_model': {'id': 'BreezeBlue/Breeze-TTS-2',
                               'revision': 'c1c8ca18b70b30822735633991d9ebf4898e47d4'},
                'adapter': {'file': artifact.name, 'sha256': digest},
                'lora': {'variant': 'backbone_depth_projection', 'rank': 8, 'alpha': 16.0, 'seed': 42}}
    config = tmp_path / 'adapter_config.json'
    config.write_text(json.dumps(manifest))
    assert adapter_metadata('') == {}
    assert adapter_metadata(tmp_path)['sha256'] == digest
    assert str(tmp_path) not in json.dumps(adapter_metadata(tmp_path))
    from runtime.media.music_reply import _speech_adapter_fingerprint
    tts_config = tmp_path / 'tts.json'
    tts_config.write_text(json.dumps({'settings': {'provider_options': {'adapter_dir': str(tmp_path)}}}))
    fingerprint = _speech_adapter_fingerprint(tts_config, {})
    assert fingerprint['sha256'] == digest
    artifact.write_bytes(b'corrupted')
    assert _speech_adapter_fingerprint(tts_config, {}) != fingerprint
    with pytest.raises(ValueError, match='BREEZE_ADAPTER_INVALID'):
        adapter_metadata(tmp_path)
    artifact.write_bytes(b'synthetic adapter')
    manifest['adapter']['file'] = '../adapter.safetensors'
    config.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='BREEZE_ADAPTER_INVALID'):
        adapter_metadata(tmp_path)


def test_additive_lora_does_not_mutate_base():
    import torch
    from tts.breeze_adapter import make_additive_lora
    base = torch.nn.Linear(4, 3)
    before = base.weight.detach().clone()
    a, b = torch.randn(2, 4), torch.randn(3, 2)
    layer = make_additive_lora(base, a, b, 2)
    x = torch.randn(5, 4)
    assert torch.allclose(layer(x), base(x) + (x @ a.T @ b.T) * 2)
    assert torch.equal(before, base.weight)


def test_provider_passes_adapter_and_rejects_missing_assets(tmp_path):
    from tts.breeze import BreezeTTS2Provider
    from tts.contracts import TTSConfig
    from voice_direction import VoicePerformancePlan
    provider = BreezeTTS2Provider(TTSConfig(provider_options={'adapter_dir': str(tmp_path)}))
    assert 'adapter' in provider._missing_files()
    with pytest.raises(ValueError, match='BREEZE_ADAPTER_INVALID'):
        provider.performance_request(VoicePerformancePlan(reply_text='测试语音。',
            overall_emotion='自然', global_speed=1.0, energy=0.5,
            breath_before_sentences=(), emphasize_sentences=()))
