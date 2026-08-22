from __future__ import annotations

import asyncio
import json
from pathlib import Path

from tools.persona_companion_acceptance import run_acceptance


ROOT = Path(__file__).resolve().parents[2]


def test_complete_persona_source_and_companion_acceptance_passes() -> None:
    result = run_acceptance(ROOT)

    assert result["status"] == "PASS", result["issues"]
    assert result["persona_status"] == "READY"
    assert result["declaration_count"] >= 30
    assert result["active_mode_prompts"] == 3
    assert result["source_sections_covered"] == 25
    assert result["source_blob_verified"] is True
    assert result["issues"] == []


def test_private_and_control_sections_never_target_the_public_release_persona() -> None:
    coverage = json.loads(
        (ROOT / "linli_character" / "persona_source_coverage_v2.json").read_text(
            encoding="utf-8"
        )
    )
    sections = {item["section_id"]: item for item in coverage["sections"]}

    for section_id in ("10", "11", "13", "15", "19"):
        item = sections[section_id]
        assert item["disposition"] in {"private_local", "future_disabled"}
        assert "persona_release" not in item["destinations"]
        assert item["public_boundary"] in {
            "no_instances",
            "no_communications",
            "no_text_triggers",
        }


def test_companion_http_send_remains_operational_without_optional_review_model(
    monkeypatch,
) -> None:
    import local_server

    local_server.store.letters.clear()
    local_server.store.request_keys.clear()
    monkeypatch.setattr(local_server, "_persist_store_state", lambda: None)
    monkeypatch.setattr(
        local_server.letters_adapter,
        "reply",
        lambda *_args: "我收到了。今天先不急着把所有事都解决。",
    )

    sent = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            {"content": "今天有点累。", "material": {}},
            {},
        )
    )

    assert sent["code"] == 0
    letter_id = sent["data"]["letter_id"]
    detail = asyncio.run(
        local_server.route("GET", "/toy/letter/detail", {}, {"letter_id": letter_id})
    )
    assert detail["code"] == 0
    assert detail["data"]["reply_text"] == "我收到了。今天先不急着把所有事都解决。"
    assert detail["data"]["letter_status"] in {4, "COMPLETED"}
    assert detail["data"]["media_status"] in {"NOT_REQUESTED", None}
