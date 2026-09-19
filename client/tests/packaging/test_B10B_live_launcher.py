from __future__ import annotations

import os

from tools.live_b10b import launcher_environment


def test_launcher_only_reads_the_real_deepseek_key_name(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "real-key-for-test")
    monkeypatch.setenv("OLIVIA_LLM_API_KEY", "must-not-be-read")

    values = launcher_environment(os.environ)

    assert values["OLIVIA_LLM_PROVIDER"] == "openai_compatible"
    assert values["OLIVIA_LLM_BASE_URL"] == "https://api.deepseek.com"
    assert values["OLIVIA_LLM_MODEL"] == "deepseek-v4-flash"
    assert values["OLIVIA_LLM_API_KEY_ENV"] == "DEEPSEEK_API_KEY"
    assert values["DEEPSEEK_API_KEY"] == "real-key-for-test"
    assert "OLIVIA_LLM_API_KEY" not in values
