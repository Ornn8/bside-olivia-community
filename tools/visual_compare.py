"""Evidence-bounded visual comparison contract for B01B.

This module measures image relationships when the required inputs exist.  It
does not turn a measurement into an acceptance decision: thresholds remain
UNFROZEN, identity requires an explicitly supplied provider, and missing
dependencies or missing evidence are represented by a three-state result.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - diagnostic path.
    cv2 = None

try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover - diagnostic path.
    np = None

try:
    from tools import visual_baseline
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools import visual_baseline  # type: ignore


MEASURED = "MEASURED"
UNAVAILABLE = "UNAVAILABLE"
UNVERIFIED = "UNVERIFIED"
METRIC_STATUSES = frozenset({MEASURED, UNAVAILABLE, UNVERIFIED})
THRESHOLDS = "UNFROZEN"


class VisualCompareError(Exception):
    """A short privacy-safe CLI error code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _result(
    metric: str,
    status: str,
    *,
    value: Any = None,
    reason: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in METRIC_STATUSES:
        raise ValueError(status)
    result: dict[str, Any] = {
        "metric": metric,
        "status": status,
        "value": value,
        "threshold_status": THRESHOLDS,
    }
    if reason is not None:
        result["reason"] = reason
    if details is not None:
        result["details"] = dict(details)
    return result


def _require_arrays() -> None:
    if cv2 is None:
        raise VisualCompareError("opencv_unavailable")
    if np is None:
        raise VisualCompareError("numpy_unavailable")


def _same_shape(reference: Any, candidate: Any) -> bool:
    return tuple(reference.shape) == tuple(candidate.shape)


def _numeric_pair(reference: Any, candidate: Any) -> tuple[Any, Any] | None:
    _require_arrays()
    if not _same_shape(reference, candidate):
        return None
    return reference.astype(np.float64), candidate.astype(np.float64)


def _color_channels(image: Any) -> Any:
    _require_arrays()
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 4:
        return image[:, :, :3]
    if image.shape[2] == 1:
        return cv2.cvtColor(image[:, :, 0], cv2.COLOR_GRAY2BGR)
    return image


def _gray(image: Any) -> Any:
    _require_arrays()
    color = _color_channels(image)
    return cv2.cvtColor(color, cv2.COLOR_BGR2GRAY)


def measure_dimensions_alpha(reference: Any, candidate: Any) -> dict[str, Any]:
    _require_arrays()
    reference_shape = list(reference.shape)
    candidate_shape = list(candidate.shape)
    reference_alpha = bool(reference.ndim == 3 and reference.shape[2] == 4)
    candidate_alpha = bool(candidate.ndim == 3 and candidate.shape[2] == 4)
    return _result(
        "dimensions_alpha",
        MEASURED,
        value={
            "same_dimensions": reference.shape[:2] == candidate.shape[:2],
            "same_channels": reference_shape[-1:] == candidate_shape[-1:],
            "reference_shape": reference_shape,
            "candidate_shape": candidate_shape,
            "reference_alpha": reference_alpha,
            "candidate_alpha": candidate_alpha,
            "same_alpha": reference_alpha == candidate_alpha,
        },
    )


def measure_exact_pixel_diff(reference: Any, candidate: Any) -> dict[str, Any]:
    pair = _numeric_pair(reference, candidate)
    if pair is None:
        return _result("exact_pixel_diff", UNVERIFIED, reason="dimension_or_channel_mismatch")
    reference_float, candidate_float = pair
    delta = np.abs(reference_float - candidate_float)
    if delta.ndim == 3:
        changed = np.any(delta != 0, axis=2)
    else:
        changed = delta != 0
    return _result(
        "exact_pixel_diff",
        MEASURED,
        value={
            "changed_pixels": int(np.count_nonzero(changed)),
            "total_pixels": int(changed.size),
            "changed_fraction": float(np.count_nonzero(changed) / max(changed.size, 1)),
            "max_abs_channel_delta": float(delta.max()) if delta.size else 0.0,
            "mean_abs_channel_delta": float(delta.mean()) if delta.size else 0.0,
        },
    )


def measure_psnr(reference: Any, candidate: Any) -> dict[str, Any]:
    pair = _numeric_pair(reference, candidate)
    if pair is None:
        return _result("psnr", UNVERIFIED, reason="dimension_or_channel_mismatch")
    reference_float, candidate_float = pair
    mse = float(np.mean((reference_float - candidate_float) ** 2))
    if mse == 0:
        return _result("psnr", MEASURED, value=None, details={"mse": 0.0, "infinite": True})
    value = 20.0 * math.log10(255.0 / math.sqrt(mse))
    return _result("psnr", MEASURED, value=float(value), details={"mse": mse, "unit": "dB"})


def measure_ssim(reference: Any, candidate: Any) -> dict[str, Any]:
    pair = _numeric_pair(reference, candidate)
    if pair is None:
        return _result("ssim", UNVERIFIED, reason="dimension_or_channel_mismatch")
    try:
        from skimage.metrics import structural_similarity  # type: ignore
    except ImportError:
        return _result("ssim", UNAVAILABLE, reason="dependency_missing:skimage")
    reference_array, candidate_array = pair
    try:
        if reference_array.ndim == 3:
            value = structural_similarity(reference_array, candidate_array, channel_axis=-1, data_range=255.0)
        else:
            value = structural_similarity(reference_array, candidate_array, data_range=255.0)
    except (TypeError, ValueError):
        return _result("ssim", UNVERIFIED, reason="ssim_input_unsupported")
    return _result("ssim", MEASURED, value=float(value), details={"data_range": 255.0})


def measure_lpips(
    reference: Any,
    candidate: Any,
    provider: Callable[[Any, Any], Any] | None = None,
) -> dict[str, Any]:
    if _numeric_pair(reference, candidate) is None:
        return _result("lpips", UNVERIFIED, reason="dimension_or_channel_mismatch")
    if provider is None:
        return _result("lpips", UNAVAILABLE, reason="provider_not_configured")
    try:
        raw = provider(reference, candidate)
    except Exception as exc:  # Provider failures must not become a fake score.
        return _result("lpips", UNAVAILABLE, reason=f"provider_error:{type(exc).__name__}")
    if isinstance(raw, Mapping):
        value = raw.get("value")
        details = {str(key): value for key, value in raw.items() if key != "value"}
    else:
        value = raw
        details = {}
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        return _result("lpips", UNVERIFIED, reason="provider_value_invalid")
    return _result("lpips", MEASURED, value=float(value), details=details)


def measure_color_difference(reference: Any, candidate: Any) -> dict[str, Any]:
    pair = _numeric_pair(reference, candidate)
    if pair is None:
        return _result("color_difference", UNVERIFIED, reason="dimension_or_channel_mismatch")
    try:
        reference_color = _color_channels(reference).astype(np.uint8)
        candidate_color = _color_channels(candidate).astype(np.uint8)
        reference_lab = cv2.cvtColor(reference_color, cv2.COLOR_BGR2LAB).astype(np.float64)
        candidate_lab = cv2.cvtColor(candidate_color, cv2.COLOR_BGR2LAB).astype(np.float64)
    except (cv2.error, ValueError):
        return _result("color_difference", UNVERIFIED, reason="color_conversion_failed")
    delta = np.linalg.norm(reference_lab - candidate_lab, axis=2)
    return _result(
        "color_difference",
        MEASURED,
        value={
            "mean_lab_distance": float(delta.mean()) if delta.size else 0.0,
            "max_lab_distance": float(delta.max()) if delta.size else 0.0,
            "color_space": "OpenCV_8bit_LAB",
        },
    )


def measure_sharpness(reference: Any, candidate: Any) -> dict[str, Any]:
    if cv2 is None or np is None:
        return _result("sharpness", UNAVAILABLE, reason="dependency_missing:opencv")
    try:
        reference_value = float(cv2.Laplacian(_gray(reference), cv2.CV_64F).var())
        candidate_value = float(cv2.Laplacian(_gray(candidate), cv2.CV_64F).var())
    except (cv2.error, ValueError):
        return _result("sharpness", UNVERIFIED, reason="sharpness_input_unsupported")
    return _result(
        "sharpness",
        MEASURED,
        value={
            "reference_laplacian_variance": reference_value,
            "candidate_laplacian_variance": candidate_value,
            "delta": candidate_value - reference_value,
        },
    )


def measure_identity_consistency(
    reference: Any,
    candidate: Any,
    provider: Callable[[Any, Any], Any] | None = None,
) -> dict[str, Any]:
    if provider is None:
        return _result("identity_consistency", UNVERIFIED, reason="manual_identity_review_required")
    try:
        raw = provider(reference, candidate)
    except Exception as exc:  # A provider failure is not an identity result.
        return _result("identity_consistency", UNAVAILABLE, reason=f"provider_error:{type(exc).__name__}")
    if isinstance(raw, Mapping) and raw.get("status") in METRIC_STATUSES:
        status = str(raw["status"])
        value = raw.get("value")
        details = {str(key): value for key, value in raw.items() if key not in {"status", "value"}}
        return _result("identity_consistency", status, value=value, details=details)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(float(raw)):
        return _result("identity_consistency", MEASURED, value=float(raw), details={"provider_contract": True})
    return _result("identity_consistency", UNVERIFIED, reason="provider_value_invalid", details={"provider_contract": True})


def measure_background_drift(reference: Any, candidate: Any, background_mask: Any | None = None) -> dict[str, Any]:
    pair = _numeric_pair(reference, candidate)
    if pair is None:
        return _result("background_drift", UNVERIFIED, reason="dimension_or_channel_mismatch")
    if background_mask is None:
        return _result("background_drift", UNVERIFIED, reason="background_mask_required")
    if background_mask.shape[:2] != reference.shape[:2]:
        return _result("background_drift", UNVERIFIED, reason="background_mask_dimension_mismatch")
    if background_mask.ndim == 3:
        mask = np.any(background_mask != 0, axis=2)
    else:
        mask = background_mask != 0
    if not np.any(mask):
        return _result("background_drift", UNVERIFIED, reason="background_mask_empty")
    reference_float, candidate_float = pair
    delta = np.abs(reference_float - candidate_float)
    if delta.ndim == 3:
        delta = delta.mean(axis=2)
    selected = delta[mask]
    return _result(
        "background_drift",
        MEASURED,
        value={
            "masked_pixels": int(np.count_nonzero(mask)),
            "mean_abs_delta": float(selected.mean()),
            "max_abs_delta": float(selected.max()),
        },
        details={"mask_semantics": "nonzero_is_background"},
    )


def measure_temporal_flicker(reference_sequence: Sequence[Any] | None, candidate_sequence: Sequence[Any] | None) -> dict[str, Any]:
    if reference_sequence is None or candidate_sequence is None:
        return _result("temporal_flicker", UNVERIFIED, reason="sequence_not_provided")
    if len(reference_sequence) != len(candidate_sequence) or len(reference_sequence) < 2:
        return _result("temporal_flicker", UNVERIFIED, reason="sequence_length_insufficient_or_mismatch")
    reference_steps: list[float] = []
    candidate_steps: list[float] = []
    for reference_a, reference_b, candidate_a, candidate_b in zip(
        reference_sequence,
        reference_sequence[1:],
        candidate_sequence,
        candidate_sequence[1:],
    ):
        reference_pair = _numeric_pair(reference_a, reference_b)
        candidate_pair = _numeric_pair(candidate_a, candidate_b)
        if reference_pair is None or candidate_pair is None:
            return _result("temporal_flicker", UNVERIFIED, reason="sequence_dimension_or_channel_mismatch")
        reference_steps.append(float(np.abs(reference_pair[0] - reference_pair[1]).mean()))
        candidate_steps.append(float(np.abs(candidate_pair[0] - candidate_pair[1]).mean()))
    return _result(
        "temporal_flicker",
        MEASURED,
        value={
            "reference_mean_frame_delta": float(np.mean(reference_steps)),
            "candidate_mean_frame_delta": float(np.mean(candidate_steps)),
            "delta": float(np.mean(candidate_steps) - np.mean(reference_steps)),
            "step_count": len(reference_steps),
        },
    )


def measure_frame_rate(reference_metadata: Mapping[str, Any] | None, candidate_metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(reference_metadata, Mapping) or not isinstance(candidate_metadata, Mapping):
        return _result("frame_rate", UNVERIFIED, reason="frame_metadata_required")
    reference_value = reference_metadata.get("frame_rate")
    candidate_value = candidate_metadata.get("frame_rate")
    if (
        not isinstance(reference_value, (int, float))
        or isinstance(reference_value, bool)
        or not isinstance(candidate_value, (int, float))
        or isinstance(candidate_value, bool)
    ):
        return _result("frame_rate", UNVERIFIED, reason="frame_rate_missing")
    if (
        not math.isfinite(float(reference_value))
        or float(reference_value) <= 0
        or not math.isfinite(float(candidate_value))
        or float(candidate_value) <= 0
    ):
        return _result("frame_rate", UNVERIFIED, reason="frame_rate_invalid")
    return _result(
        "frame_rate",
        MEASURED,
        value={
            "reference_fps": float(reference_value),
            "candidate_fps": float(candidate_value),
            "delta_fps": float(candidate_value - reference_value),
        },
    )


def measure_av_sync(reference_metadata: Mapping[str, Any] | None, candidate_metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(reference_metadata, Mapping) or not isinstance(candidate_metadata, Mapping):
        return _result("av_sync", UNVERIFIED, reason="av_metadata_required")
    reference_value = reference_metadata.get("av_sync_offset_seconds")
    candidate_value = candidate_metadata.get("av_sync_offset_seconds")
    if (
        not isinstance(reference_value, (int, float))
        or isinstance(reference_value, bool)
        or not isinstance(candidate_value, (int, float))
        or isinstance(candidate_value, bool)
    ):
        return _result("av_sync", UNVERIFIED, reason="av_sync_offset_missing")
    return _result(
        "av_sync",
        MEASURED,
        value={
            "reference_offset_seconds": float(reference_value),
            "candidate_offset_seconds": float(candidate_value),
            "delta_seconds": float(candidate_value - reference_value),
        },
    )


def compare_frames(
    reference: Any,
    candidate: Any,
    *,
    reference_metadata: Mapping[str, Any] | None = None,
    candidate_metadata: Mapping[str, Any] | None = None,
    background_mask: Any | None = None,
    reference_sequence: Sequence[Any] | None = None,
    candidate_sequence: Sequence[Any] | None = None,
    lpips_provider: Callable[[Any, Any], Any] | None = None,
    identity_provider: Callable[[Any, Any], Any] | None = None,
) -> dict[str, Any]:
    _require_arrays()
    metrics = [
        measure_dimensions_alpha(reference, candidate),
        measure_exact_pixel_diff(reference, candidate),
        measure_psnr(reference, candidate),
        measure_ssim(reference, candidate),
        measure_lpips(reference, candidate, lpips_provider),
        measure_color_difference(reference, candidate),
        measure_sharpness(reference, candidate),
        measure_identity_consistency(reference, candidate, identity_provider),
        measure_background_drift(reference, candidate, background_mask),
        measure_temporal_flicker(reference_sequence, candidate_sequence),
        measure_frame_rate(reference_metadata, candidate_metadata),
        measure_av_sync(reference_metadata, candidate_metadata),
    ]
    status_counts: dict[str, int] = {MEASURED: 0, UNAVAILABLE: 0, UNVERIFIED: 0}
    for metric in metrics:
        status_counts[metric["status"]] += 1
    return {
        "schema_version": 1,
        "comparison_kind": "visual_comparison_contract",
        "acceptance_status": UNVERIFIED,
        "thresholds": THRESHOLDS,
        "identity_policy": "REPLACEABLE_PROVIDER_HOOK_NO_GENERIC_FACE_MODEL",
        "manual_total_control_view_required": True,
        "metrics": metrics,
        "metric_status_counts": status_counts,
    }


def _read_evidence_image(value: str | Path, repo_root: Path) -> Any:
    path = visual_baseline._private_input(value, repo_root)
    _require_arrays()
    try:
        encoded = np.fromfile(str(path), dtype=np.uint8)
        image = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
    except (OSError, cv2.error) as exc:
        raise VisualCompareError("image_decode_failed") from exc
    if image is None or getattr(image, "size", 0) == 0:
        raise VisualCompareError("image_decode_failed")
    return image


def _read_metadata(value: str | Path | None, repo_root: Path) -> Mapping[str, Any] | None:
    if value is None:
        return None
    path = visual_baseline._private_input(value, repo_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VisualCompareError("metadata_read") from exc
    if not isinstance(payload, Mapping):
        raise VisualCompareError("metadata_invalid")
    return payload


def _read_sequence(values: Sequence[str] | None, repo_root: Path) -> list[Any] | None:
    if values is None:
        return None
    return [_read_evidence_image(value, repo_root) for value in values]


def _cmd_compare(args: argparse.Namespace) -> int:
    try:
        repo_root = visual_baseline.asset_manifest._repo_root(args.repo_root)
        reference = _read_evidence_image(args.reference, repo_root)
        candidate = _read_evidence_image(args.candidate, repo_root)
        background_mask = _read_evidence_image(args.background_mask, repo_root) if args.background_mask else None
        reference_metadata = _read_metadata(args.reference_metadata, repo_root)
        candidate_metadata = _read_metadata(args.candidate_metadata, repo_root)
        reference_sequence = _read_sequence(args.reference_sequence, repo_root)
        candidate_sequence = _read_sequence(args.candidate_sequence, repo_root)
        document = compare_frames(
            reference,
            candidate,
            reference_metadata=reference_metadata,
            candidate_metadata=candidate_metadata,
            background_mask=background_mask,
            reference_sequence=reference_sequence,
            candidate_sequence=candidate_sequence,
        )
        output = visual_baseline._private_output(args.output, repo_root)
        if output in {visual_baseline._private_output(args.reference, repo_root), visual_baseline._private_output(args.candidate, repo_root)}:
            raise VisualCompareError("output_collision")
        visual_baseline._write_json(output, document)
    except (visual_baseline.asset_manifest.ManifestError, visual_baseline.VisualBaselineError) as exc:
        raise VisualCompareError(getattr(exc, "code", "input_error")) from exc
    print("status=COMPLETE")
    print("acceptance_status=UNVERIFIED")
    print(f"metrics={len(document['metrics'])}")
    print("private_output_written=1")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)
    compare = subparsers.add_parser("compare", help="compare two private evidence frames")
    compare.add_argument("--reference", required=True)
    compare.add_argument("--candidate", required=True)
    compare.add_argument("--output", required=True)
    compare.add_argument("--reference-metadata")
    compare.add_argument("--candidate-metadata")
    compare.add_argument("--background-mask")
    compare.add_argument("--reference-sequence", action="append")
    compare.add_argument("--candidate-sequence", action="append")
    compare.set_defaults(handler=_cmd_compare)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (VisualCompareError, visual_baseline.VisualBaselineError) as exc:
        print(f"status=ERROR:{getattr(exc, 'code', 'error')}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
