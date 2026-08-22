from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from tools import asset_manifest, visual_baseline


ROOT = Path(__file__).resolve().parents[1]


def _write_image(path: Path, value: int = 32, *, alpha: bool = False) -> None:
    if visual_baseline.cv2 is None:
        pytest.skip("OpenCV is required for image fixture")
    path.parent.mkdir(parents=True, exist_ok=True)
    channels = 4 if alpha else 3
    image = np.full((6, 8, channels), value, dtype=np.uint8)
    assert visual_baseline.cv2.imwrite(str(path), image)


def _private_manifest(tmp_path: Path, source: Path) -> dict:
    return asset_manifest.scan_roots({"fixture": source})


def test_image_timestamp_zero_extracts_png_metadata_and_hash(tmp_path: Path) -> None:
    source = tmp_path / "source"
    image_path = source / "private-original.png"
    _write_image(image_path, alpha=True)
    manifest = _private_manifest(tmp_path, source)
    item = manifest["items"][0]
    output = tmp_path / "frames" / "frame.png"
    metadata = tmp_path / "frames" / "frame.json"

    record = visual_baseline.extract_frame(
        manifest,
        {"fixture": source},
        item["logical_id"],
        0.0,
        output,
        metadata,
    )

    assert record["status"] == "EXTRACTED"
    assert record["alpha_present"] is True
    assert record["width"] == 8
    assert record["height"] == 6
    assert record["png_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert json.loads(metadata.read_text(encoding="utf-8"))["source_logical_id"] == item["logical_id"]


def test_image_timestamp_boundary_and_negative_timestamp_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_image(source / "frame.png")
    manifest = _private_manifest(tmp_path, source)
    logical_id = manifest["items"][0]["logical_id"]
    with pytest.raises(visual_baseline.VisualBaselineError, match="image_timestamp_out_of_range"):
        visual_baseline.extract_frame(
            manifest,
            {"fixture": source},
            logical_id,
            0.001,
            tmp_path / "frame.png",
            tmp_path / "frame.json",
        )
    with pytest.raises(visual_baseline.VisualBaselineError, match="timestamp_invalid"):
        visual_baseline._parse_timestamp(-0.001)


def test_bad_media_and_missing_opencv_have_explicit_diagnostics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    bad = source / "bad.png"
    bad.write_bytes(b"not-a-png")
    manifest = _private_manifest(tmp_path, source)
    logical_id = manifest["items"][0]["logical_id"]
    with pytest.raises(visual_baseline.VisualBaselineError, match="image_decode_failed"):
        visual_baseline.extract_frame(
            manifest,
            {"fixture": source},
            logical_id,
            0.0,
            tmp_path / "bad-frame.png",
            tmp_path / "bad-frame.json",
        )

    monkeypatch.setattr(visual_baseline, "cv2", None)
    with pytest.raises(visual_baseline.VisualBaselineError, match="opencv_unavailable"):
        visual_baseline.extract_frame(
            manifest,
            {"fixture": source},
            logical_id,
            0.0,
            tmp_path / "missing.png",
            tmp_path / "missing.json",
        )


def test_source_write_is_forbidden_and_output_must_stay_ignored() -> None:
    source = ROOT / ".evidence" / "synthetic-source"
    source.mkdir(parents=True, exist_ok=True)
    (source / "source.png").write_bytes(b"synthetic")
    with pytest.raises(asset_manifest.ManifestError, match="output_boundary"):
        asset_manifest.ensure_private_output_path(ROOT / "not-private.json", ROOT)
    with pytest.raises(visual_baseline.VisualBaselineError, match="source_write_forbidden"):
        visual_baseline._assert_output_not_source(source / "source.png", source / "source.png", {"fixture": source})
    with pytest.raises(visual_baseline.VisualBaselineError, match="output_boundary"):
        visual_baseline._private_output(ROOT / ".evidence" / ".." / "escaped.json", ROOT)


def test_state_candidates_cover_required_states_without_filename_verification() -> None:
    paths = [
        "A_R1_1200.mp4",
        "A_R1_1730.mp4",
        "A_R1_2000.mp4",
        "assets_home_idle.mp4",
        "midi_performance.mp4",
        "postcard_user.webp",
        "postcard_letter.webp",
        "assets_mode-interactive.webp",
        "A_R2_1200.mp4",
        "A_R3_1200.mp4",
        "A_Transition_1200_1730.mp4",
    ]
    items = []
    for relative in paths:
        category = "image" if relative.endswith(".webp") else "video"
        items.append(
            {
                "logical_id": asset_manifest.logical_id("game", category, relative),
                "root_alias": "game",
                "relative_path": relative,
                "extension": Path(relative).suffix,
                "category": category,
                "bytes": 1,
                "sha256": "0" * 64,
                "media_metadata": {"image": None, "video": None, "audio": None},
                "probe_status": "unavailable" if category == "video" else "error",
                "reason": "probe_tool_unavailable" if category == "video" else "invalid_media",
            }
        )
    document = visual_baseline.build_state_candidates(
        {
            "roots": [{"alias": "game", "item_count": len(items)}],
            "items": items,
        }
    )
    assert {unit["state_id"] for unit in document["state_units"]} == set(visual_baseline.EXPECTED_STATE_IDS)
    assert all(unit["status"] in {"CANDIDATE", "BLOCKED", "UNVERIFIED"} for unit in document["state_units"])
    assert not any(unit["status"] == "VERIFIED" for unit in document["state_units"])
    assert visual_baseline.validate_state_matrix_document(document) == ()


def test_state_matrix_requires_manual_review_for_verified_and_tracks_evidence() -> None:
    candidates = visual_baseline.synthetic_state_matrix_document()
    frame = {"state_id": "day", "frame_id": "day_shot_001"}
    matrix = visual_baseline.apply_frame_evidence(candidates, [frame])
    day = next(unit for unit in matrix["state_units"] if unit["state_id"] == "day")
    assert day["status"] == "UNVERIFIED"
    assert day["evidence_count"] == 1
    assert visual_baseline.validate_state_matrix_document(matrix) == ()

    day["status"] = "VERIFIED"
    assert "verified_requires_manual_review" in visual_baseline.validate_state_matrix_document(matrix)
    day["verification_method"] = "manual_review"
    assert visual_baseline.validate_state_matrix_document(matrix) == ()


def test_contact_sheet_is_private_and_frame_index_is_utf8(tmp_path: Path) -> None:
    frame_dir = tmp_path / "frames"
    first = frame_dir / "frame_0001.png"
    second = frame_dir / "frame_0002.png"
    _write_image(first, 20)
    _write_image(second, 80)
    records = [
        {"source_logical_id": "asset_" + "1" * 32, "requested_timestamp_seconds": 0.0, "output_file": first.name},
        {"source_logical_id": "asset_" + "2" * 32, "requested_timestamp_seconds": 1.0, "output_file": second.name},
    ]
    sheet = tmp_path / "contact-sheet.png"
    metadata = tmp_path / "contact-sheet.json"
    result = visual_baseline.create_contact_sheet(records, frame_dir, sheet, metadata)
    assert result["frame_count"] == 2
    assert result["png_sha256"] == hashlib.sha256(sheet.read_bytes()).hexdigest()
    assert json.loads(metadata.read_text(encoding="utf-8"))["record_kind"] == visual_baseline.CONTACT_SHEET_KIND


def test_sanitized_summary_does_not_leak_path_filename_or_hash(tmp_path: Path) -> None:
    secret_path = str(tmp_path / "private-root")
    secret_name = "user-private-original.mp4"
    secret_hash = "a" * 64
    manifest = {
        "roots": [{"alias": "game", "item_count": 1}],
        "items": [
            {
                "root_alias": "game",
                "relative_path": secret_name,
                "category": "video",
                "sha256": secret_hash,
            }
        ],
    }
    summary = visual_baseline.sanitized_summary_document(
        manifest,
        visual_baseline.synthetic_state_matrix_document(),
        frame_count=0,
        contact_sheet_count=0,
    )
    encoded = json.dumps(summary, ensure_ascii=False)
    assert secret_path not in encoded
    assert secret_name not in encoded
    assert secret_hash not in encoded
    assert "relative_path" not in encoded
    assert "sha256" not in encoded


def test_schema_and_synthetic_example_match_code_contract() -> None:
    schema = json.loads((ROOT / "visual_state_matrix.schema.json").read_text(encoding="utf-8"))
    example = json.loads((ROOT / "visual_state_matrix.example.json").read_text(encoding="utf-8"))
    assert schema == visual_baseline.state_matrix_schema_document()
    assert example == visual_baseline.synthetic_state_matrix_document()


def test_schema_explicitly_requires_every_expected_state() -> None:
    schema = json.loads((ROOT / "visual_state_matrix.schema.json").read_text(encoding="utf-8"))
    covered = {
        block["contains"]["properties"]["state_id"]["const"]
        for block in schema["allOf"]
    }
    assert covered == set(visual_baseline.EXPECTED_STATE_IDS)
    assert all(block["minContains"] == 1 for block in schema["allOf"])


def test_evidence_package_is_utf8_and_manifest_excludes_self(tmp_path: Path) -> None:
    input_dir = tmp_path / "run"
    input_dir.mkdir()
    (input_dir / "record.json").write_text('{"status":"UNVERIFIED"}\n', encoding="utf-8")
    package_path = input_dir / "evidence-package.json"
    manifest_path = input_dir / "manifest.sha256"
    package = visual_baseline.build_evidence_package(input_dir, package_path, manifest_path)
    assert package["encoding"] == "UTF-8"
    assert package["candidate_media_generated"] is False
    assert package["manifest_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert "record.json" in manifest_text
    assert "manifest.sha256" not in manifest_text
    assert "evidence-package.json" not in manifest_text
