from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from tools import visual_compare
from tools.visual_driver import build_evidence_report, main as visual_driver_main, measure_region_integrity
from visual_driver import (
    DRIVEN,
    FALLBACK,
    ORIGINAL_FRAME_FALLBACK,
    OriginalVisualFrame,
    VisualDriver,
    VisualDriverError,
    VisualDriverRequest,
    VISUAL_STATE_IDS,
    unavailable_av_sync,
)


ROOT = Path(__file__).resolve().parents[2]
ASSET_REF = "asset_4d1c44521d987dde8e6bd6bf0b0fd4f5"


def _manifest() -> dict:
    return {
        "schema_version": 1,
        "manifest_kind": "private_asset_manifest",
        "tool_version": "1",
        "roots": [{"alias": "fixture", "item_count": 1}],
        "items": [{
            "logical_id": ASSET_REF,
            "root_alias": "fixture",
            "relative_path": "live.mp4",
            "extension": ".mp4",
            "category": "video",
            "bytes": 1,
            "sha256": "b" * 64,
            "media_metadata": {"image": None, "video": None, "audio": None},
            "probe_status": "unavailable",
            "reason": "probe_tool_unavailable",
        }],
    }


def _original(state_id: str = "live") -> OriginalVisualFrame:
    frame = np.arange(8 * 8 * 3, dtype=np.uint8).reshape((8, 8, 3))
    return OriginalVisualFrame(
        state_id=state_id,
        asset_ref=ASSET_REF,
        frame=frame,
        frame_index=4,
        timestamp_seconds=0.133333,
        frame_rate=30.0,
        asset_manifest=_manifest(),
        metadata={"source": "synthetic_fixture"},
    )


def _speaking_mask() -> np.ndarray:
    mask = np.zeros((8, 8), dtype=np.uint8)
    mask[3:5, 3:5] = 1
    return mask


def _protected_regions() -> dict[str, np.ndarray]:
    regions: dict[str, np.ndarray] = {}
    for index, region_id in enumerate(("face", "hair", "clothing", "skin", "framing", "background", "lighting", "clarity")):
        mask = np.zeros((8, 8), dtype=np.uint8)
        mask[index // 4, index % 4] = 1
        regions[region_id] = mask
    return regions


class SpeakingBackend:
    def __call__(self, request: VisualDriverRequest) -> np.ndarray:
        candidate = request.original.frame.copy()
        candidate[3:5, 3:5] = 255
        return candidate


class MutatingBackend:
    def __call__(self, request: VisualDriverRequest) -> np.ndarray:
        request.original.frame[:] = 0
        return request.original.frame


def test_driver_only_writes_unprotected_speaking_pixels() -> None:
    original = _original()
    result = VisualDriver(SpeakingBackend()).render(
        VisualDriverRequest(
            original=original,
            speaking_mask=_speaking_mask(),
            protected_regions=_protected_regions(),
        )
    )
    assert result.status == DRIVEN
    assert result.fallback_reason is None
    assert result.active_pixel_count == 4
    assert result.protected_pixel_count == 8
    assert np.array_equal(result.frame[:3], original.frame[:3])
    assert np.array_equal(result.frame[5:], original.frame[5:])
    assert np.array_equal(result.frame[:, :3], original.frame[:, :3])
    assert np.all(result.frame[3:5, 3:5] == 255)
    public = result.public_dict()
    assert "frame" not in public
    assert "asset_ref" not in public
    assert public["media_written"] is False
    assert public["original_visual_policy"]["replacement_media_generated"] is False

    original_before = original.frame.copy()
    mutated = VisualDriver(MutatingBackend()).render(
        VisualDriverRequest(
            original=original,
            speaking_mask=_speaking_mask(),
            protected_regions=_protected_regions(),
        )
    )
    assert mutated.status == DRIVEN
    assert np.array_equal(original.frame, original_before)
    assert np.array_equal(mutated.frame[:3], original_before[:3])


def test_missing_backend_and_backend_failures_return_the_original_frame() -> None:
    original = _original()
    request = VisualDriverRequest(original=original, speaking_mask=_speaking_mask())
    no_backend = VisualDriver().render(request)
    assert no_backend.status == FALLBACK
    assert no_backend.fallback_reason == "driver_unavailable"
    assert no_backend.output_source == ORIGINAL_FRAME_FALLBACK
    assert np.array_equal(no_backend.frame, original.frame)

    def failing_backend(_request: VisualDriverRequest) -> np.ndarray:
        raise RuntimeError("synthetic failure")

    failed = VisualDriver(failing_backend).render(request)
    assert failed.status == FALLBACK
    assert failed.fallback_reason == "backend_error"
    assert np.array_equal(failed.frame, original.frame)

    invalid = VisualDriver(lambda _request: np.zeros((2, 2, 3), dtype=np.uint8)).render(request)
    assert invalid.status == FALLBACK
    assert invalid.fallback_reason == "backend_output_invalid"
    assert np.array_equal(invalid.frame, original.frame)

    rejected = VisualDriver(SpeakingBackend(), quality_guard=lambda _request, _frame: False).render(request)
    assert rejected.status == FALLBACK
    assert rejected.fallback_reason == "quality_guard_failed"
    assert np.array_equal(rejected.frame, original.frame)


def test_protection_or_missing_mask_is_a_fallback_not_a_fake_success() -> None:
    original = _original()
    no_mask = VisualDriver(SpeakingBackend()).render(VisualDriverRequest(original=original))
    assert no_mask.status == FALLBACK
    assert no_mask.fallback_reason == "speaking_mask_missing"

    fully_protected = {"hair": np.ones((8, 8), dtype=np.uint8)}
    protected = VisualDriver(SpeakingBackend()).render(
        VisualDriverRequest(original=original, speaking_mask=_speaking_mask(), protected_regions=fully_protected)
    )
    assert protected.status == FALLBACK
    assert protected.fallback_reason == "speaking_region_protected"
    assert np.array_equal(protected.frame, original.frame)


def test_original_frame_contract_rejects_paths_and_invalid_states() -> None:
    with pytest.raises(VisualDriverError, match="original_asset_reference_invalid"):
        OriginalVisualFrame(state_id="live", asset_ref="C:\\private\\frame.png", frame=np.zeros((2, 2, 3), dtype=np.uint8))
    with pytest.raises(VisualDriverError, match="state_invalid"):
        OriginalVisualFrame(state_id="unknown", asset_ref=ASSET_REF, frame=np.zeros((2, 2, 3), dtype=np.uint8))

    with pytest.raises(VisualDriverError, match="original_manifest_required"):
        OriginalVisualFrame(state_id="live", asset_ref=ASSET_REF, frame=np.zeros((2, 2, 3), dtype=np.uint8))

    nonfinite = _original().frame.astype(np.float32)
    nonfinite[0, 0, 0] = np.nan
    with pytest.raises(VisualDriverError, match="original_frame_nonfinite"):
        OriginalVisualFrame(
            state_id="live",
            asset_ref=ASSET_REF,
            frame=nonfinite,
            asset_manifest=_original().asset_manifest,
        )


def test_state_coverage_is_complete_and_deterministic() -> None:
    coverage = VisualDriver().coverage({"live": _original()})
    assert coverage["state_count"] == 10
    assert [unit["state_id"] for unit in coverage["state_units"]] == list(VISUAL_STATE_IDS)
    assert coverage["available_state_count"] == 1
    assert coverage["missing_state_ids"] == [state for state in VISUAL_STATE_IDS if state != "live"]
    assert all(unit["fallback_source"] == "original_frame" for unit in coverage["state_units"])
    assert coverage["original_visual_policy"]["candidate_media_generated"] is False
    with pytest.raises(VisualDriverError, match="state_input_invalid"):
        VisualDriver().coverage({"live": None})


def test_av_sync_boundary_is_stable_and_truthfully_unavailable() -> None:
    first = unavailable_av_sync()
    second = unavailable_av_sync()
    assert first == second
    assert first == {
        "metric": "av_sync",
        "status": "UNAVAILABLE",
        "value": None,
        "threshold_status": "UNFROZEN",
        "reason": "b05_b06_runtime_unavailable",
        "source": "b05_b06_contract",
    }


def test_public_frame_metadata_is_not_reemitted() -> None:
    frame = OriginalVisualFrame(
        state_id="live",
        asset_ref=ASSET_REF,
        frame=np.zeros((2, 2, 3), dtype=np.uint8),
        asset_manifest=_original().asset_manifest,
        metadata={"source_path": "C:\\private\\frame.png", "secret": "do-not-return"},
    )
    public = frame.public_dict()
    assert "metadata" not in public
    assert "source_path" not in json.dumps(public)


def test_evidence_report_covers_required_metrics_without_measuring_av_sync() -> None:
    original = _original()
    candidate = original.frame.copy()
    candidate[3:5, 3:5] = 255
    region_masks = {region_id: np.ones((8, 8), dtype=np.uint8) for region_id in (
        "face", "hair", "clothing", "skin", "framing", "background", "lighting", "clarity"
    )}
    report = build_evidence_report(
        original.frame,
        candidate,
        state_id="live",
        reference_metadata={"frame_rate": 30.0, "av_sync_offset_seconds": 0.0},
        candidate_metadata={"frame_rate": 30.0, "av_sync_offset_seconds": 99.0},
        background_mask=np.ones((8, 8), dtype=np.uint8),
        region_masks=region_masks,
        reference_sequence=[original.frame, original.frame],
        candidate_sequence=[original.frame, candidate],
        manifest=_manifest(),
        reference_source_metadata={"record_kind": "private_visual_frame", "source_logical_id": ASSET_REF},
        candidate_source_metadata={"record_kind": "private_visual_frame", "source_logical_id": ASSET_REF},
    )
    assert report["acceptance_status"] == "UNVERIFIED"
    assert report["thresholds"] == "UNFROZEN"
    assert set(report["required_metrics"]) == {
        "pixel", "ssim", "lpips", "identity", "flicker", "background_drift", "color", "fps", "av_sync"
    }
    assert report["required_metrics"]["fps"]["status"] == visual_compare.MEASURED
    assert report["required_metrics"]["av_sync"] == unavailable_av_sync()
    assert report["metrics"][-1]["metric"] == "region_integrity"
    assert report["metrics"][-1]["status"] == visual_compare.MEASURED
    assert report["verification_boundary"]["replacement_media_committed"] is False
    assert visual_compare.measure_frame_rate(
        {"frame_rate": float("nan")}, {"frame_rate": 30.0}
    )["status"] == visual_compare.UNVERIFIED


def test_region_integrity_requires_explicit_masks() -> None:
    original = _original().frame
    report = measure_region_integrity(original, original)
    assert report["status"] == "UNVERIFIED"
    assert all(value["reason"] == "region_mask_required" for value in report["value"]["regions"].values())


def test_private_evidence_cli_writes_json_only(tmp_path: Path) -> None:
    assert visual_compare.cv2 is not None
    frame_dir = tmp_path / "b07"
    frame_dir.mkdir()
    reference_path = frame_dir / "reference.png"
    candidate_path = frame_dir / "candidate.png"
    reference = np.zeros((4, 4, 3), dtype=np.uint8)
    candidate = np.ones((4, 4, 3), dtype=np.uint8)
    assert visual_compare.cv2.imwrite(str(reference_path), reference)
    assert visual_compare.cv2.imwrite(str(candidate_path), candidate)
    output = frame_dir / "report.json"
    manifest_path = frame_dir / "manifest.json"
    reference_metadata_path = frame_dir / "reference.json"
    candidate_metadata_path = frame_dir / "candidate.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
    reference_metadata_path.write_text(
        json.dumps({"record_kind": "private_visual_frame", "source_logical_id": ASSET_REF, "frame_rate": 30.0}), encoding="utf-8"
    )
    candidate_metadata_path.write_text(
        json.dumps({"record_kind": "private_visual_frame", "source_logical_id": ASSET_REF, "frame_rate": 30.0}), encoding="utf-8"
    )
    assert visual_driver_main(
        [
            "--repo-root", str(ROOT),
            "report",
            "--state-id", "live",
            "--manifest", str(manifest_path),
            "--reference", str(reference_path),
            "--candidate", str(candidate_path),
            "--reference-metadata", str(reference_metadata_path),
            "--candidate-metadata", str(candidate_metadata_path),
            "--output", str(output),
        ]
    ) == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["report_kind"] == "b07_visual_driver_evidence"
    assert document["verification_boundary"]["source_paths_in_report"] is False
    assert not list(frame_dir.glob("*.mp4"))


def test_private_coverage_cli_reports_all_states(tmp_path: Path) -> None:
    output = tmp_path / "coverage.json"
    assert visual_driver_main(
        [
            "--repo-root", str(ROOT),
            "coverage",
            "--available-state", "live",
            "--output", str(output),
        ]
    ) == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["state_count"] == 10
    assert len(document["state_units"]) == 10
    assert document["verification_boundary"]["av_sync"] == unavailable_av_sync()


def test_schema_and_example_are_path_free_and_cover_the_contract() -> None:
    schema = json.loads((ROOT / "contracts" / "visual_driver.schema.json").read_text(encoding="utf-8"))
    example = json.loads((ROOT / "contracts" / "visual_driver.example.json").read_text(encoding="utf-8"))
    assert schema["properties"]["report_kind"]["const"] == "b07_visual_driver_evidence"
    assert len(example["state_coverage"]) == 10
    assert set(example["required_metrics"]) == {
        "pixel", "ssim", "lpips", "identity", "flicker", "background_drift", "color", "fps", "av_sync"
    }
    serialized = json.dumps(example, ensure_ascii=False)
    assert "asset_" not in serialized
    assert "\\\\" not in serialized
    assert ":\\\\" not in serialized
