from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from tools import asset_manifest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _assert_repo_basetemp(path: Path) -> None:
    evidence = (REPO_ROOT / ".evidence").resolve()
    assert path.resolve().is_relative_to(evidence)
    assert "<WINDOWS_PATH>" not in str(path).lower()


def _write_png(path: Path, width: int = 3, height: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )
    path.write_bytes(payload)


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 8)


def _run(command: list[str]) -> int:
    return asset_manifest.main(["--repo-root", str(REPO_ROOT), *command])


def test_scan_is_stable_and_records_hashes_duplicates_and_metadata(tmp_path: Path, capsys) -> None:
    _assert_repo_basetemp(tmp_path)
    root = tmp_path / "synthetic-original"
    _write_png(root / "nested" / "portrait.png")
    _write_wav(root / "voice.wav")
    (root / "copy-a.bin").write_bytes(b"same synthetic asset")
    (root / "copy-b.bin").write_bytes(b"same synthetic asset")

    output_a = tmp_path / "manifest-a.json"
    output_b = tmp_path / "manifest-b.json"
    assert _run(["scan", "--root", f"original={root}", "--output", str(output_a)]) == 0
    first_log = capsys.readouterr().out
    assert _run(["scan", "--root", f"original={root}", "--output", str(output_b)]) == 0
    second_log = capsys.readouterr().out

    first = json.loads(output_a.read_text(encoding="utf-8"))
    second = json.loads(output_b.read_text(encoding="utf-8"))
    assert first == second
    assert len(first["items"]) == 4
    assert {item["logical_id"] for item in first["items"]}.__len__() == 4
    assert all(item["sha256"] for item in first["items"])
    png = next(item for item in first["items"] if item["extension"] == ".png")
    assert png["media_metadata"]["image"]["width"] == 3
    assert png["media_metadata"]["image"]["height"] == 2
    assert png["probe_status"] == "ok"
    wav = next(item for item in first["items"] if item["extension"] == ".wav")
    assert wav["media_metadata"]["audio"]["sample_rate"] == 8000
    assert wav["probe_status"] == "ok"
    assert "private_output_written=1" in first_log
    assert "private_output_written=1" in second_log

    assert _run(["validate", "--manifest", str(output_a), "--root", f"original={root}"]) == 0
    validation_log = capsys.readouterr().out
    assert "status=PASS" in validation_log
    assert "duplicate_sha256_groups=1" in validation_log
    assert str(root) not in validation_log


def test_multiple_roots_and_summary_are_count_only(tmp_path: Path) -> None:
    _assert_repo_basetemp(tmp_path)
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "player-private-name.txt").write_text("synthetic player data", encoding="utf-8")
    (root_b / "same.txt").write_text("synthetic player data", encoding="utf-8")
    output = tmp_path / "multi-root.json"
    assert _run(
        [
            "scan",
            "--root",
            f"alpha={root_a}",
            "--root",
            f"beta={root_b}",
            "--output",
            str(output),
        ]
    ) == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    summary = asset_manifest.build_sanitized_summary(manifest)
    encoded = json.dumps(summary, ensure_ascii=False)
    assert summary["schema_version"] == 1
    assert summary["counts"]["by_alias"] == {"alpha": 1, "beta": 1}
    assert summary["counts"]["by_extension"] == {".txt": 2}
    assert "player-private-name.txt" not in encoded
    assert "synthetic player data" not in encoded
    assert "relative_path" not in encoded
    assert "sha256" not in encoded
    assert str(root_a) not in encoded


def test_logs_and_committed_example_do_not_leak_paths_or_media(tmp_path: Path, capsys) -> None:
    _assert_repo_basetemp(tmp_path)
    root = tmp_path / "source-with-private-path"
    secret_name = "player-private-name.txt"
    secret_content = "synthetic player data that must stay local"
    root.mkdir()
    (root / secret_name).write_text(secret_content, encoding="utf-8")
    output = tmp_path / "private.json"

    assert _run(["scan", "--root", f"private={root}", "--output", str(output)]) == 0
    log = capsys.readouterr().out
    assert str(root) not in log
    assert secret_name not in log
    assert secret_content not in log

    example_text = (REPO_ROOT / "asset_manifest.example.json").read_text(encoding="utf-8")
    assert secret_name not in example_text
    assert "relative_path" not in example_text
    assert "sha256" not in example_text
    assert str(root) not in example_text


def test_validate_rejects_path_escape_windows_path_and_bad_hash() -> None:
    item = {
        "logical_id": asset_manifest.logical_id("fixture", "image", "../escape.png"),
        "root_alias": "fixture",
        "relative_path": "../escape.png",
        "extension": ".png",
        "category": "image",
        "bytes": 1,
        "sha256": "0" * 64,
        "media_metadata": asset_manifest._empty_media_metadata(),
        "probe_status": "error",
        "reason": "invalid_media",
    }
    manifest = {
        "schema_version": 1,
        "manifest_kind": "private_asset_manifest",
        "tool_version": "1",
        "roots": [{"alias": "fixture", "item_count": 1}],
        "items": [item],
    }
    report = asset_manifest.validate_manifest_document(manifest)
    assert "path_escape" in report.issues
    windows_path = "Z" + ":" + chr(92) + "fixture" + chr(92) + "escape.png"
    assert not asset_manifest._safe_relative_path(windows_path)
    assert not asset_manifest._safe_relative_path(r"folder\file.png")

    item["relative_path"] = windows_path
    item["logical_id"] = asset_manifest.logical_id("fixture", "image", item["relative_path"])
    item["sha256"] = "bad"
    report = asset_manifest.validate_manifest_document(manifest)
    assert "path_escape" in report.issues
    assert "hash_format" in report.issues


def test_bad_media_is_recorded_and_missing_file_is_validated(tmp_path: Path, capsys) -> None:
    _assert_repo_basetemp(tmp_path)
    root = tmp_path / "media"
    root.mkdir()
    bad_media = root / "broken.png"
    bad_media.write_bytes(b"not a real image")
    output = tmp_path / "media.json"
    assert _run(["scan", "--root", f"media={root}", "--output", str(output)]) == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    item = manifest["items"][0]
    assert item["probe_status"] == "error"
    assert item["reason"] == "invalid_media"

    bad_media.unlink()
    assert _run(["validate", "--manifest", str(output), "--root", f"media={root}"]) == 1
    log = capsys.readouterr().out
    assert "status=FAIL" in log
    assert "missing_files=1" in log
    assert str(root) not in log
    assert "broken.png" not in log


def test_schema_and_example_match_cli_documents() -> None:
    schema_path = REPO_ROOT / "asset_manifest.schema.json"
    example_path = REPO_ROOT / "asset_manifest.example.json"
    assert json.loads(schema_path.read_text(encoding="utf-8")) == asset_manifest.schema_document()
    assert json.loads(example_path.read_text(encoding="utf-8")) == asset_manifest.example_document()


def test_private_output_requires_explicit_ignored_evidence_boundary(tmp_path: Path, capsys) -> None:
    _assert_repo_basetemp(tmp_path)
    accepted = tmp_path / "accepted.json"
    assert asset_manifest.ensure_private_output_path(accepted, REPO_ROOT) == accepted.resolve()

    outside = REPO_ROOT / "not-private.json"
    with pytest.raises(asset_manifest.ManifestError, match="output_boundary"):
        asset_manifest.ensure_private_output_path(outside, REPO_ROOT)
    escaped = REPO_ROOT / ".evidence" / ".." / "escaped.json"
    with pytest.raises(asset_manifest.ManifestError, match="output_boundary"):
        asset_manifest.ensure_private_output_path(escaped, REPO_ROOT)

    source = tmp_path / "source"
    source.mkdir()
    (source / "secret.txt").write_text("secret content", encoding="utf-8")
    assert _run(["scan", "--root", f"source={source}", "--output", str(outside)]) == 2
    log = capsys.readouterr().out
    assert "output_boundary" in log
    assert str(source) not in log
