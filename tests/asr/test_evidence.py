from __future__ import annotations

import json
from pathlib import Path

from asr.config import AsrConfig
from tools.build_b05_evidence import build


ROOT = Path(__file__).resolve().parents[2]


def test_evidence_fixture_is_playable_and_explicitly_not_native(tmp_path: Path) -> None:
    manifest = build(tmp_path, AsrConfig(provider="nemotron-speech-cpp").with_test_paths(tmp_path))
    audio = Path(manifest["audio"]["path"])
    assert audio.is_file()
    assert audio.read_bytes()[:4] == b"RIFF"
    assert manifest["evidence_class"] == "offline-contract-fixture"
    assert manifest["native_acceptance"] is False
    assert json.loads(Path(manifest["events"]).read_text(encoding="utf-8"))["native_run"] is False


def test_pytest_temp_root_is_nested_and_cannot_clear_b05_evidence() -> None:
    pytest_config = (ROOT / "pytest.ini").read_text(encoding="utf-8")
    assert "addopts = --basetemp=.evidence/pytest" in pytest_config
    assert "addopts = --basetemp=.evidence\n" not in pytest_config
