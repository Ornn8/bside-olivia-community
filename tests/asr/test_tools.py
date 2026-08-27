from __future__ import annotations

import json
from pathlib import Path

import pytest

from asr.config import AsrConfig
from tools import asr_healthcheck, asr_manage


def test_asr_healthcheck_default_text_fallback_is_zero(capsys) -> None:
    assert asr_healthcheck.main([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"]["provider"] == "text-fallback"
    assert payload["status"]["status"] == "available"


def test_asr_healthcheck_require_ready_rejects_unverified_native(tmp_path: Path, capsys) -> None:
    config = AsrConfig(provider="nemotron-speech-cpp").with_test_paths(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config.to_dict()), encoding="utf-8")
    assert asr_healthcheck.main(["--config", str(config_path), "--require-ready"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"]["reason"] == "ASR_RUNTIME_MISSING"


def test_asr_manage_switch_cli_writes_only_config(capsys, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    assert asr_manage.main(["switch", "--config", str(config_path), "--provider", "text-fallback"]) == 0
    json.loads(capsys.readouterr().out)
    assert AsrConfig.from_json(config_path).provider == "text-fallback"


def test_asr_manage_rejects_relative_data_root_before_resolution(capsys, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        asr_manage.main(
            [
                "switch",
                "--config",
                str(tmp_path / "config.json"),
                "--provider",
                "text-fallback",
                "--data-root",
                "relative/asr",
            ]
        )

    assert "--data-root must be an absolute local Windows path" in capsys.readouterr().err
