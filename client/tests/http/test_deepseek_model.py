"""DeepSeek Flash is a selectable relay model (2.5.0): listed, reported in telemetry, streamed with usage."""
import json
from pathlib import Path

from original_client_relay_api import RELAY_MODELS
from runtime.diagnostics.reply_telemetry import MODELS
from runtime.reply.model_capabilities import model_capabilities


def test_deepseek_flash_can_be_chosen_and_reported():
    assert 'deepseek-flash' in RELAY_MODELS and 'deepseek-flash' in MODELS
    capabilities = model_capabilities('https://api.bside-moon.cn/v1', 'deepseek-flash')
    assert capabilities.stream_usage and capabilities.json_mode and capabilities.thinking == 'none'
    for name in ('reply_telemetry.schema.json', 'reply_telemetry_v2.schema.json'):
        text = (Path(__file__).resolve().parents[2] / 'contracts' / name).read_text(encoding='utf-8')
        assert '"deepseek-flash"' in text
        json.loads(text)
