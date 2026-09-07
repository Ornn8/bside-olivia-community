import hashlib
import json

import pytest

from runtime.imports import offline_letter_pairs as subject


@pytest.mark.parametrize("whitespace", ["\r", "\n", "\t", "\r\n"])
def test_raw_string_whitespace_preserves_text_and_source_digest(tmp_path, whitespace):
    path = tmp_path / "synthetic.json"
    raw = ('[{"content":"first' + whitespace + 'second","reply":"reply"}]').encode()
    path.write_bytes(raw)
    parsed_raw, pairs = subject._parse_source(path)
    assert parsed_raw == raw and path.read_bytes() == raw
    assert pairs == (("first" + whitespace + "second", "reply"),)
    report = subject._recovery_plan_from_hashes(parsed_raw, pairs, set())
    assert report.source_sha256 == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("code", [code for code in range(32) if code not in (9, 10, 13)])
def test_other_raw_control_characters_remain_invalid(tmp_path, code):
    path = tmp_path / "synthetic.json"
    path.write_bytes(b'[{"content":"first' + bytes([code]) + b'second","reply":"reply"}]')
    with pytest.raises(ValueError, match="OFFLINE_LETTER_SOURCE_INVALID"):
        subject._parse_source(path)


@pytest.mark.parametrize("raw", [
    b'[{"content":"first\nsecond","reply":"reply",}]',
    b'[{"content":"first\nsecond","reply":"reply"}] trailing',
    b'[{"content":"first\nsecond","reply":1}]',
    b'[{"content":"first\nsecond","reply":"reply","extra":true}]',
])
def test_relaxed_whitespace_keeps_syntax_and_field_validation(tmp_path, raw):
    path = tmp_path / "synthetic.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        subject._parse_source(path)


def test_relaxed_whitespace_keeps_both_size_limits(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.json"
    raw = b'[{"content":"first\nsecond","reply":"reply"}]'
    path.write_bytes(raw)
    monkeypatch.setattr(subject, "_MAX_SOURCE_BYTES", len(raw) - 1)
    with pytest.raises(ValueError, match="OFFLINE_LETTER_SOURCE_TOO_LARGE"):
        subject._parse_source(path)
    monkeypatch.setattr(subject, "_MAX_SOURCE_BYTES", len(raw))
    monkeypatch.setattr(subject, "_MAX_PAIR_CHARS", 1)
    with pytest.raises(ValueError, match="OFFLINE_LETTER_PAIR_TOO_LARGE"):
        subject._parse_source(path)


def test_escaped_control_characters_keep_existing_json_behavior(tmp_path):
    path = tmp_path / "synthetic.json"
    path.write_text(json.dumps([{"content": "first\x00second", "reply": "reply"}]))
    assert subject._parse_source(path)[1][0][0] == "first\x00second"
