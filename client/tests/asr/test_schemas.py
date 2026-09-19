from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_b05_schemas_are_versioned_and_match_contract_constants() -> None:
    events = json.loads((ROOT / "contracts" / "asr_events.schema.json").read_text(encoding="utf-8"))
    config = json.loads((ROOT / "contracts" / "asr_config.schema.json").read_text(encoding="utf-8"))
    assert events["$id"] == "b05.asr.events.v1"
    assert "partial" in events["properties"]["type"]["enum"]
    assert "final" in events["properties"]["type"]["enum"]
    assert config["properties"]["model_revision"]["const"]
    assert config["properties"]["runtime_revision"]["const"]
