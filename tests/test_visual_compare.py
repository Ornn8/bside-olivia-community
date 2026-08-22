from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from tools import visual_compare


def _metric(document: dict, name: str) -> dict:
    return next(metric for metric in document["metrics"] if metric["metric"] == name)


def test_identical_frames_measure_exact_diff_and_psnr_without_acceptance_pass() -> None:
    reference = np.zeros((5, 6, 3), dtype=np.uint8)
    document = visual_compare.compare_frames(reference, reference.copy())
    assert document["acceptance_status"] == "UNVERIFIED"
    assert document["thresholds"] == "UNFROZEN"
    assert _metric(document, "exact_pixel_diff")["status"] == visual_compare.MEASURED
    assert _metric(document, "exact_pixel_diff")["value"]["changed_pixels"] == 0
    assert _metric(document, "psnr")["status"] == visual_compare.MEASURED
    assert _metric(document, "psnr")["value"] is None
    assert _metric(document, "identity_consistency")["status"] == visual_compare.UNVERIFIED


def test_pixel_change_and_dimension_mismatch_are_not_fake_zero_or_pass() -> None:
    reference = np.zeros((5, 6, 3), dtype=np.uint8)
    changed = reference.copy()
    changed[2, 3, 0] = 255
    changed_document = visual_compare.compare_frames(reference, changed)
    assert _metric(changed_document, "exact_pixel_diff")["value"]["changed_pixels"] == 1
    assert _metric(changed_document, "color_difference")["status"] == visual_compare.MEASURED

    mismatched = np.zeros((4, 6, 3), dtype=np.uint8)
    mismatch_document = visual_compare.compare_frames(reference, mismatched)
    assert _metric(mismatch_document, "dimensions_alpha")["value"]["same_dimensions"] is False
    assert _metric(mismatch_document, "exact_pixel_diff")["status"] == visual_compare.UNVERIFIED
    assert _metric(mismatch_document, "psnr")["value"] is None


def test_optional_dependencies_and_provider_hooks_keep_three_state_contract(monkeypatch) -> None:
    reference = np.zeros((4, 4, 3), dtype=np.uint8)
    candidate = np.ones((4, 4, 3), dtype=np.uint8)
    lpips = visual_compare.measure_lpips(reference, candidate)
    assert lpips["status"] == visual_compare.UNAVAILABLE
    assert lpips["value"] is None
    assert visual_compare.measure_lpips(reference, candidate, lambda _a, _b: 0.125)["status"] == visual_compare.MEASURED

    identity = visual_compare.measure_identity_consistency(reference, candidate, lambda _a, _b: 0.75)
    assert identity["status"] == visual_compare.MEASURED
    assert identity["value"] == 0.75
    assert visual_compare.measure_identity_consistency(reference, candidate)["status"] == visual_compare.UNVERIFIED

    monkeypatch.setattr(visual_compare, "cv2", None)
    sharpness = visual_compare.measure_sharpness(reference, candidate)
    assert sharpness["status"] == visual_compare.UNAVAILABLE


def test_background_temporal_frame_rate_and_av_sync_require_explicit_evidence() -> None:
    reference = np.zeros((4, 4, 3), dtype=np.uint8)
    candidate = np.ones((4, 4, 3), dtype=np.uint8)
    mask = np.ones((4, 4), dtype=np.uint8)
    assert visual_compare.measure_background_drift(reference, candidate, mask)["status"] == visual_compare.MEASURED
    assert visual_compare.measure_background_drift(reference, candidate)["status"] == visual_compare.UNVERIFIED
    assert visual_compare.measure_temporal_flicker(None, None)["status"] == visual_compare.UNVERIFIED
    sequence = [reference, candidate]
    assert visual_compare.measure_temporal_flicker(sequence, sequence)["status"] == visual_compare.MEASURED
    metadata = {"frame_rate": 30.0, "av_sync_offset_seconds": 0.01}
    frame_rate = visual_compare.measure_frame_rate(metadata, {"frame_rate": 29.0})
    assert frame_rate["status"] == visual_compare.MEASURED
    assert visual_compare.measure_av_sync(metadata, metadata)["status"] == visual_compare.MEASURED
    assert visual_compare.measure_av_sync(metadata, {"frame_rate": 30.0})["status"] == visual_compare.UNVERIFIED


def test_all_contract_metrics_are_three_state_values() -> None:
    reference = np.zeros((3, 3, 3), dtype=np.uint8)
    document = visual_compare.compare_frames(reference, reference.copy())
    expected = {
        "exact_pixel_diff",
        "dimensions_alpha",
        "psnr",
        "ssim",
        "lpips",
        "color_difference",
        "sharpness",
        "identity_consistency",
        "background_drift",
        "temporal_flicker",
        "frame_rate",
        "av_sync",
    }
    assert {metric["metric"] for metric in document["metrics"]} == expected
    assert all(metric["status"] in visual_compare.METRIC_STATUSES for metric in document["metrics"])
    assert all("threshold_status" in metric and metric["threshold_status"] == "UNFROZEN" for metric in document["metrics"])


def test_compare_cli_writes_only_private_evidence(tmp_path: Path) -> None:
    if visual_compare.cv2 is None:
        return
    frame_dir = tmp_path / "frames"
    frame_dir.mkdir()
    reference_path = frame_dir / "reference.png"
    candidate_path = frame_dir / "candidate.png"
    reference = np.zeros((4, 4, 3), dtype=np.uint8)
    candidate = np.ones((4, 4, 3), dtype=np.uint8)
    assert visual_compare.cv2.imwrite(str(reference_path), reference)
    assert visual_compare.cv2.imwrite(str(candidate_path), candidate)
    output = tmp_path / "comparison.json"
    assert visual_compare.main(
        [
            "--repo-root",
            str(Path(__file__).resolve().parents[1]),
            "compare",
            "--reference",
            str(reference_path),
            "--candidate",
            str(candidate_path),
            "--output",
            str(output),
        ]
    ) == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["acceptance_status"] == "UNVERIFIED"
