import json
from types import SimpleNamespace

import pytest

import persona_loader
from runtime.private_world.daily_life_runtime import life_persona


def test_life_persona_preserves_validated_authority_without_rereading_file(monkeypatch, tmp_path):
    selected = SimpleNamespace(declaration_id="anchor.reading", source_id="PUBLIC.SHUTDOWN.ANNOUNCEMENT",
        tier="COMMUNITY_SOFT_CANON", confidence="MEDIUM", statement="喜欢读随笔。")
    unrelated = SimpleNamespace(declaration_id="unrelated", statement="不应带入")
    monkeypatch.setattr(persona_loader, "load_persona", lambda path: SimpleNamespace(
        snapshot=SimpleNamespace(status="READY", declarations=(selected, unrelated))))
    # The validated snapshot owns the read; a second raw file read can differ.
    result = json.loads(life_persona(tmp_path / "validated-snapshot.json"))
    assert result == [{"declaration_id": selected.declaration_id,
        "tier": selected.tier,
        "confidence": selected.confidence, "statement": selected.statement}]
    # Provenance stays in the asset; identifiers are not character knowledge.
    assert selected.source_id not in json.dumps(result)


@pytest.mark.parametrize("status", ["DRAFT", "POLICY_ONLY", "READY"])
def test_life_persona_rejects_unavailable_or_empty_background(monkeypatch, tmp_path, status):
    monkeypatch.setattr(persona_loader, "load_persona", lambda path: SimpleNamespace(
        snapshot=SimpleNamespace(status=status, declarations=())))
    with pytest.raises(ValueError, match="^DAILY_LIFE_PERSONA_UNAVAILABLE$"):
        life_persona(tmp_path / "absent.json")
