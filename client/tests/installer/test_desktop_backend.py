from __future__ import annotations

import json
from pathlib import Path

from jsonschema import validate

import desktop_backend


def test_desktop_ready_message_matches_contract() -> None:
    message = desktop_backend.desktop_ready_message(port=49152, pid=1234, nonce="a" * 48)
    schema = json.loads(
        (Path(__file__).parents[2] / "contracts" / "desktop_handshake.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validate(message, schema)
    assert json.loads(json.dumps(message)) == message


def test_desktop_ready_message_rejects_invalid_port() -> None:
    try:
        desktop_backend.desktop_ready_message(port=0, pid=1234, nonce="nonce")
    except ValueError as exc:
        assert str(exc) == "DESKTOP_PORT_INVALID"
    else:
        raise AssertionError("invalid port was accepted")


def test_desktop_update_status_keeps_manifest_field(monkeypatch) -> None:
    monkeypatch.setenv("OLIVIA_DESKTOP_VERSION", "2.5.8")
    payload = desktop_backend.desktop_update_status()
    assert set(payload) == {"version", "manifest_sha256"}
    assert payload["version"] == "2.5.8"
