"""Build reproducible, private B07 visual-driver evidence.

The command compares an original B01 frame with an in-memory driver result.
Inputs and output are restricted to the ignored ``.evidence`` directory.  It
does not encode a candidate frame, install a backend, or make an acceptance
decision.  Missing optional evidence remains ``UNAVAILABLE`` or
``UNVERIFIED`` instead of becoming a zero or a pass.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover - diagnostic environments only.
    np = None

try:
    from tools import visual_baseline, visual_compare
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools import visual_baseline, visual_compare  # type: ignore

from visual_driver import (
    REGION_IDS,
    SCHEMA_VERSION,
    UNAVAILABLE,
    UNFROZEN,
    UNVERIFIED,
    VISUAL_STATE_IDS,
    VisualDriverError,
    state_coverage_document,
    unavailable_av_sync,
)


REPORT_KIND = "b07_visual_driver_evidence"
REGION_STATUS_METRIC = "region_integrity"


def _metric(document: Mapping[str, Any], name: str) -> dict[str, Any]:
    for value in document.get("metrics", []):
        if isinstance(value, Mapping) and value.get("metric") == name:
            return dict(value)
    raise VisualDriverError(f"metric_missing:{name}")


def _mask(value: Any, shape: tuple[int, int]) -> Any:
    if np is None:
        raise VisualDriverError("numpy_unavailable", retryable=True)
    try:
        array = np.asarray(value)
    except (TypeError, ValueError) as exc:
        raise VisualDriverError("region_mask_invalid") from exc
    if array.ndim == 3:
        array = np.any(array != 0, axis=2)
    if array.ndim != 2 or tuple(array.shape) != shape:
        raise VisualDriverError("region_mask_invalid")
    return array != 0


def measure_region_integrity(
    reference: Any,
    candidate: Any,
    region_masks: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Measure the eight requested visual regions without semantic guessing."""

    if np is None:
        regions = {
            region_id: {
                "region": region_id,
                "status": UNAVAILABLE,
                "value": None,
                "reason": "numpy_unavailable",
            }
            for region_id in REGION_IDS
        }
        return {
            "metric": REGION_STATUS_METRIC,
            "status": UNAVAILABLE,
            "value": {"regions": regions},
            "threshold_status": UNFROZEN,
        }
    reference_array = np.asarray(reference)
    candidate_array = np.asarray(candidate)
    if reference_array.shape != candidate_array.shape:
        regions = {
            region_id: {
                "region": region_id,
                "status": UNVERIFIED,
                "value": None,
                "reason": "dimension_or_channel_mismatch",
            }
            for region_id in REGION_IDS
        }
        return {
            "metric": REGION_STATUS_METRIC,
            "status": UNVERIFIED,
            "value": {"regions": regions},
            "threshold_status": UNFROZEN,
        }

    masks = region_masks or {}
    shape = (int(reference_array.shape[0]), int(reference_array.shape[1]))
    regions: dict[str, dict[str, Any]] = {}
    status_counts: Counter[str] = Counter()
    for region_id in REGION_IDS:
        raw_mask = masks.get(region_id)
        if raw_mask is None:
            result = {
                "region": region_id,
                "status": UNVERIFIED,
                "value": None,
                "reason": "region_mask_required",
            }
            regions[region_id] = result
            status_counts[UNVERIFIED] += 1
            continue
        try:
            selected_mask = _mask(raw_mask, shape)
        except VisualDriverError as exc:
            result = {
                "region": region_id,
                "status": UNVERIFIED,
                "value": None,
                "reason": exc.code,
            }
            regions[region_id] = result
            status_counts[UNVERIFIED] += 1
            continue
        selected_count = int(np.count_nonzero(selected_mask))
        if selected_count == 0:
            result = {
                "region": region_id,
                "status": UNVERIFIED,
                "value": None,
                "reason": "region_mask_empty",
            }
            regions[region_id] = result
            status_counts[UNVERIFIED] += 1
            continue
        delta = np.abs(reference_array.astype(np.float64) - candidate_array.astype(np.float64))
        if delta.ndim == 3:
            delta = delta.mean(axis=2)
        selected = delta[selected_mask]
        result = {
            "region": region_id,
            "status": visual_compare.MEASURED,
            "value": {
                "masked_pixels": selected_count,
                "changed_pixels": int(np.count_nonzero(selected != 0)),
                "changed_fraction": float(np.count_nonzero(selected != 0) / selected_count),
                "mean_abs_delta": float(selected.mean()),
                "max_abs_delta": float(selected.max()),
            },
            "reason": None,
        }
        regions[region_id] = result
        status_counts[visual_compare.MEASURED] += 1
    overall = visual_compare.MEASURED if all(
        value["status"] == visual_compare.MEASURED for value in regions.values()
    ) else UNVERIFIED
    return {
        "metric": REGION_STATUS_METRIC,
        "status": overall,
        "value": {
            "regions": regions,
            "region_ids": list(REGION_IDS),
            "mask_semantics": "nonzero_selects_region",
        },
        "threshold_status": UNFROZEN,
        "status_counts": dict(status_counts),
    }


def _without_av_sync(metadata: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(metadata, Mapping):
        return metadata
    return {
        str(key): value
        for key, value in metadata.items()
        if key not in {"av_sync_offset_seconds", "av_sync", "sync_offset_seconds"}
    }


def _metric_status_counts(metrics: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for metric in metrics:
        status = metric.get("status")
        if isinstance(status, str):
            counts[status] += 1
    return dict(sorted(counts.items()))


def build_evidence_report(
    reference: Any,
    candidate: Any,
    *,
    state_id: str | None = None,
    reference_metadata: Mapping[str, Any] | None = None,
    candidate_metadata: Mapping[str, Any] | None = None,
    background_mask: Any | None = None,
    region_masks: Mapping[str, Any] | None = None,
    reference_sequence: Sequence[Any] | None = None,
    candidate_sequence: Sequence[Any] | None = None,
    lpips_provider: Any | None = None,
    identity_provider: Any | None = None,
    manifest: Mapping[str, Any] | None = None,
    reference_source_metadata: Mapping[str, Any] | None = None,
    candidate_source_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a path-free B07 report from explicitly supplied frame evidence."""

    if not isinstance(manifest, Mapping):
        raise VisualDriverError("original_manifest_required")
    try:
        manifest_report = visual_baseline.asset_manifest.validate_manifest_document(dict(manifest))
    except Exception as exc:
        raise VisualDriverError("manifest_invalid") from exc
    if not manifest_report.ok:
        raise VisualDriverError("manifest_invalid")
    _validate_source_metadata(reference_source_metadata, manifest)
    _validate_source_metadata(candidate_source_metadata, manifest)
    if state_id is not None and state_id not in VISUAL_STATE_IDS:
        raise VisualDriverError("state_invalid")
    base = visual_compare.compare_frames(
        reference,
        candidate,
        reference_metadata=_without_av_sync(reference_metadata),
        candidate_metadata=_without_av_sync(candidate_metadata),
        background_mask=background_mask,
        reference_sequence=reference_sequence,
        candidate_sequence=candidate_sequence,
        lpips_provider=lpips_provider,
        identity_provider=identity_provider,
    )
    metrics = [
        dict(metric)
        for metric in base["metrics"]
        if metric.get("metric") != "av_sync"
    ]
    metrics.append(unavailable_av_sync())
    region_metric = measure_region_integrity(reference, candidate, region_masks)
    metrics.append(region_metric)
    required_metric_map = {
        "pixel": _metric({"metrics": metrics}, "exact_pixel_diff"),
        "ssim": _metric({"metrics": metrics}, "ssim"),
        "lpips": _metric({"metrics": metrics}, "lpips"),
        "identity": _metric({"metrics": metrics}, "identity_consistency"),
        "flicker": _metric({"metrics": metrics}, "temporal_flicker"),
        "background_drift": _metric({"metrics": metrics}, "background_drift"),
        "color": _metric({"metrics": metrics}, "color_difference"),
        "fps": _metric({"metrics": metrics}, "frame_rate"),
        "av_sync": _metric({"metrics": metrics}, "av_sync"),
    }
    coverage = state_coverage_document({state_id} if state_id else set())
    return {
        "schema_version": SCHEMA_VERSION,
        "report_kind": REPORT_KIND,
        "acceptance_status": UNVERIFIED,
        "thresholds": UNFROZEN,
        "state_id": state_id,
        "state_coverage": coverage["state_units"],
        "metrics": metrics,
        "required_metrics": required_metric_map,
        "metric_status_counts": _metric_status_counts(metrics),
        "verification_boundary": {
            "original_visual_input_only": True,
            "source_kind": "b01_private_manifest_reference",
            "source_paths_in_report": False,
            "replacement_media_generated": False,
            "replacement_media_committed": False,
            "manual_total_control_view_required": True,
            "identity_requires_manual_review_without_provider": True,
            "thresholds": UNFROZEN,
            "av_sync": unavailable_av_sync(),
        },
    }


def build_coverage_report(available_states: Sequence[str] | None = None) -> dict[str, Any]:
    available = set(available_states or ())
    return {
        **state_coverage_document(available),
        "verification_boundary": {
            "original_visual_input_only": True,
            "source_paths_in_report": False,
            "candidate_media_generated": False,
            "candidate_media_committed": False,
            "av_sync": unavailable_av_sync(),
        },
    }


def _read_metadata(value: str | Path | None, repo_root: Path) -> Mapping[str, Any] | None:
    if value is None:
        return None
    path = visual_baseline._private_input(value, repo_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VisualDriverError("metadata_read") from exc
    if not isinstance(payload, Mapping):
        raise VisualDriverError("metadata_invalid")
    return payload


def _read_manifest(value: str | Path, repo_root: Path) -> Mapping[str, Any]:
    path = visual_baseline._private_input(value, repo_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        report = visual_baseline.asset_manifest.validate_manifest_document(payload)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VisualDriverError("manifest_read") from exc
    if not isinstance(payload, Mapping) or not report.ok:
        raise VisualDriverError("manifest_invalid")
    return payload


def _validate_source_metadata(metadata: Mapping[str, Any] | None, manifest: Mapping[str, Any]) -> None:
    if not isinstance(metadata, Mapping):
        raise VisualDriverError("source_metadata_required")
    if metadata.get("record_kind") != "private_visual_frame":
        raise VisualDriverError("source_metadata_invalid")
    logical_id = metadata.get("source_logical_id")
    matches = [
        item
        for item in manifest.get("items", [])
        if isinstance(item, Mapping) and item.get("logical_id") == logical_id
    ]
    if len(matches) != 1 or matches[0].get("category") not in {"image", "video"}:
        raise VisualDriverError("source_metadata_not_original_visual")


def _read_sequence(values: Sequence[str] | None, repo_root: Path) -> list[Any] | None:
    if values is None:
        return None
    return [visual_compare._read_evidence_image(value, repo_root) for value in values]


def _parse_region_masks(values: Sequence[str] | None, repo_root: Path) -> dict[str, Any]:
    masks: dict[str, Any] = {}
    for value in values or ():
        if "=" not in value:
            raise VisualDriverError("region_mask_spec")
        region_id, path_value = value.split("=", 1)
        if region_id not in REGION_IDS or not path_value or region_id in masks:
            raise VisualDriverError("region_mask_spec")
        masks[region_id] = visual_compare._read_evidence_image(path_value, repo_root)
    return masks


def _cmd_report(args: argparse.Namespace) -> int:
    repo_root = visual_baseline.asset_manifest._repo_root(args.repo_root)
    manifest = _read_manifest(args.manifest, repo_root)
    reference = visual_compare._read_evidence_image(args.reference, repo_root)
    candidate = visual_compare._read_evidence_image(args.candidate, repo_root)
    background_mask = (
        visual_compare._read_evidence_image(args.background_mask, repo_root)
        if args.background_mask
        else None
    )
    reference_metadata = _read_metadata(args.reference_metadata, repo_root)
    candidate_metadata = _read_metadata(args.candidate_metadata, repo_root)
    _validate_source_metadata(reference_metadata, manifest)
    _validate_source_metadata(candidate_metadata, manifest)
    report = build_evidence_report(
        reference,
        candidate,
        state_id=args.state_id,
        reference_metadata=reference_metadata,
        candidate_metadata=candidate_metadata,
        background_mask=background_mask,
        region_masks=_parse_region_masks(args.region_mask, repo_root),
        reference_sequence=_read_sequence(args.reference_sequence, repo_root),
        candidate_sequence=_read_sequence(args.candidate_sequence, repo_root),
        manifest=manifest,
        reference_source_metadata=reference_metadata,
        candidate_source_metadata=candidate_metadata,
    )
    output = visual_baseline._private_output(args.output, repo_root)
    input_paths = {
        visual_baseline._private_output(args.reference, repo_root),
        visual_baseline._private_output(args.candidate, repo_root),
    }
    if output in input_paths:
        raise VisualDriverError("output_collision")
    visual_baseline._write_json(output, report)
    print("status=COMPLETE")
    print(f"report_kind={REPORT_KIND}")
    print(f"metrics={len(report['metrics'])}")
    print("av_sync=UNAVAILABLE")
    print("replacement_media_written=0")
    return 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    repo_root = visual_baseline.asset_manifest._repo_root(args.repo_root)
    report = build_coverage_report(args.available_state)
    output = visual_baseline._private_output(args.output, repo_root)
    visual_baseline._write_json(output, report)
    print("status=COMPLETE")
    print("state_count=10")
    print("replacement_media_written=0")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    report = subparsers.add_parser("report", help="write private B07 comparison evidence")
    report.add_argument("--reference", required=True)
    report.add_argument("--candidate", required=True)
    report.add_argument("--output", required=True)
    report.add_argument("--manifest", required=True)
    report.add_argument("--state-id", choices=VISUAL_STATE_IDS)
    report.add_argument("--reference-metadata")
    report.add_argument("--candidate-metadata")
    report.add_argument("--background-mask")
    report.add_argument("--region-mask", action="append")
    report.add_argument("--reference-sequence", action="append")
    report.add_argument("--candidate-sequence", action="append")
    report.set_defaults(handler=_cmd_report)

    coverage = subparsers.add_parser("coverage", help="write path-free state coverage evidence")
    coverage.add_argument("--available-state", action="append", choices=VISUAL_STATE_IDS)
    coverage.add_argument("--output", required=True)
    coverage.set_defaults(handler=_cmd_coverage)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        VisualDriverError,
        visual_compare.VisualCompareError,
        visual_baseline.VisualBaselineError,
        visual_baseline.asset_manifest.ManifestError,
    ) as exc:
        print(f"status=ERROR:{getattr(exc, 'code', 'input_error')}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
