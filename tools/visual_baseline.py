"""Private, logical-ID-only visual baseline tooling for B01B.

The command reads a B01A private manifest and explicitly supplied source roots.
It never accepts a source path as an asset selector, never writes to a source
root, and writes media only below the repository's ignored ``.evidence/``
directory.  State classification is deliberately conservative: path/name
signals can create candidates, but cannot create a VERIFIED state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - exercised through the diagnostic path.
    cv2 = None

try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover - exercised through the diagnostic path.
    np = None

try:
    from tools import asset_manifest
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools import asset_manifest  # type: ignore


SCHEMA_VERSION = 1
MATRIX_KIND = "visual_state_matrix"
FRAME_INDEX_KIND = "private_visual_frame_index"
CONTACT_SHEET_KIND = "private_visual_contact_sheet"
STATUS_VALUES = frozenset({"VERIFIED", "UNVERIFIED", "BLOCKED", "CANDIDATE"})
EXPECTED_STATE_IDS = (
    "day",
    "dusk",
    "night",
    "idle",
    "piano_performance",
    "letter_reply",
    "letter_reading",
    "live",
    "outfit_variants",
    "scene_transitions",
)
NOTES_CODE_RE = re.compile(r"^[A-Z0-9_]+$")
LOGICAL_ID_RE = re.compile(r"^asset_[0-9a-f]{32}$")
STATE_ID_RE = re.compile(r"^[a-z][a-z0-9_]+$")


class VisualBaselineError(Exception):
    """A short, privacy-safe error code for the CLI."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError) as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise VisualBaselineError("output_write") from exc


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VisualBaselineError("json_read") from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise VisualBaselineError("read_file") from exc
    return digest.hexdigest()


def _require_cv2() -> None:
    if cv2 is None:
        raise VisualBaselineError("opencv_unavailable")
    if np is None:
        raise VisualBaselineError("numpy_unavailable")


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def _private_output(value: str | Path, repo_root: Path) -> Path:
    try:
        return asset_manifest.ensure_private_output_path(value, repo_root)
    except asset_manifest.ManifestError as exc:
        raise VisualBaselineError(exc.code) from exc


def _private_input(value: str | Path, repo_root: Path) -> Path:
    """Read an ignored evidence input without accepting an arbitrary path."""

    path = _private_output(value, repo_root)
    if not path.is_file():
        raise VisualBaselineError("evidence_input_missing")
    return path


def _parse_timestamp(value: str | float | int) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError) as exc:
        raise VisualBaselineError("timestamp_invalid") from exc
    if not math.isfinite(timestamp) or timestamp < 0:
        raise VisualBaselineError("timestamp_invalid")
    return timestamp


def _load_registered_manifest(
    manifest_value: str | Path,
    roots_specs: Sequence[str],
    repo_root: Path,
) -> tuple[dict[str, Any], dict[str, Path]]:
    manifest_path = _private_input(manifest_value, repo_root)
    try:
        manifest = _read_json(manifest_path)
        roots = asset_manifest.parse_root_specs(roots_specs)
        report = asset_manifest.validate_manifest_document(manifest, roots)
    except asset_manifest.ManifestError as exc:
        raise VisualBaselineError(exc.code) from exc
    if not report.ok:
        raise VisualBaselineError("manifest_invalid")
    if not isinstance(manifest, dict):
        raise VisualBaselineError("manifest_invalid")
    return manifest, roots


def _item_by_logical_id(manifest: Mapping[str, Any], logical_id: str) -> Mapping[str, Any]:
    if not LOGICAL_ID_RE.fullmatch(logical_id):
        raise VisualBaselineError("logical_id_invalid")
    matches = [
        item
        for item in manifest.get("items", [])
        if isinstance(item, dict) and item.get("logical_id") == logical_id
    ]
    if len(matches) != 1:
        raise VisualBaselineError("logical_id_unknown")
    return matches[0]


def _source_path_for_item(item: Mapping[str, Any], roots: Mapping[str, Path]) -> Path:
    alias = item.get("root_alias")
    relative_path = item.get("relative_path")
    if not isinstance(alias, str) or alias not in roots:
        raise VisualBaselineError("root_alias_unknown")
    if not isinstance(relative_path, str) or not asset_manifest._safe_relative_path(relative_path):
        raise VisualBaselineError("path_escape")
    try:
        root = Path(roots[alias]).resolve(strict=True)
        candidate = root.joinpath(*relative_path.split("/"))
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise VisualBaselineError("source_unavailable") from exc
    if not _is_within(resolved, root) or not resolved.is_file():
        raise VisualBaselineError("path_escape")
    return resolved


def _assert_output_not_source(output: Path, source: Path, roots: Mapping[str, Path]) -> None:
    try:
        resolved_output = output.resolve(strict=False)
        resolved_source = source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise VisualBaselineError("output_boundary") from exc
    if resolved_output == resolved_source:
        raise VisualBaselineError("source_write_forbidden")
    for root_value in roots.values():
        try:
            root = Path(root_value).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise VisualBaselineError("root_path") from exc
        if _is_within(resolved_output, root):
            raise VisualBaselineError("source_write_forbidden")


def _read_image(path: Path) -> Any:
    _require_cv2()
    try:
        encoded = np.fromfile(str(path), dtype=np.uint8)
        image = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
    except (OSError, cv2.error) as exc:
        raise VisualBaselineError("image_decode_failed") from exc
    if image is None or getattr(image, "size", 0) == 0:
        raise VisualBaselineError("image_decode_failed")
    return image


def _png_bytes(image: Any) -> bytes:
    _require_cv2()
    try:
        ok, encoded = cv2.imencode(".png", image)
    except cv2.error as exc:
        raise VisualBaselineError("png_encode_failed") from exc
    if not ok or encoded is None:
        raise VisualBaselineError("png_encode_failed")
    return bytes(encoded)


def _write_bytes(path: Path, value: bytes, *, overwrite: bool = False) -> None:
    if path.exists() and not overwrite:
        raise VisualBaselineError("output_exists")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            handle.write(value)
    except OSError as exc:
        raise VisualBaselineError("output_write") from exc


def _shape(image: Any) -> list[int]:
    if getattr(image, "ndim", 0) == 2:
        return [int(image.shape[0]), int(image.shape[1]), 1]
    return [int(image.shape[0]), int(image.shape[1]), int(image.shape[2])]


def _alpha_present(image: Any) -> bool:
    return len(_shape(image)) == 3 and _shape(image)[2] == 4


def _extract_image_frame(
    item: Mapping[str, Any],
    source: Path,
    timestamp: float,
    output: Path,
    metadata_output: Path,
    roots: Mapping[str, Path],
) -> dict[str, Any]:
    if timestamp != 0:
        raise VisualBaselineError("image_timestamp_out_of_range")
    image = _read_image(source)
    encoded = _png_bytes(image)
    _assert_output_not_source(output, source, roots)
    _write_bytes(output, encoded)
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "private_visual_frame",
        "source_logical_id": item["logical_id"],
        "source_root_alias": item["root_alias"],
        "source_category": item["category"],
        "requested_timestamp_seconds": 0.0,
        "actual_timestamp_seconds": 0.0,
        "frame_index": 0,
        "frame_rate": None,
        "frame_count": 1,
        "duration_seconds": 0.0,
        "width": int(image.shape[1]),
        "height": int(image.shape[0]),
        "channels": _shape(image)[2],
        "alpha_present": _alpha_present(image),
        "png_sha256": _sha256_bytes(encoded),
        "png_bytes": len(encoded),
        "output_file": output.name,
        "status": "EXTRACTED",
    }
    _write_json(metadata_output, record)
    return record


def _extract_video_frame(
    item: Mapping[str, Any],
    source: Path,
    timestamp: float,
    output: Path,
    metadata_output: Path,
    roots: Mapping[str, Path],
) -> dict[str, Any]:
    _require_cv2()
    capture = cv2.VideoCapture(str(source))
    try:
        if not capture.isOpened():
            raise VisualBaselineError("video_open_failed")
        frame_rate = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count_float = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
        if not math.isfinite(frame_rate) or frame_rate <= 0:
            raise VisualBaselineError("video_frame_rate_unavailable")
        if not math.isfinite(frame_count_float) or frame_count_float <= 0:
            raise VisualBaselineError("video_frame_count_unavailable")
        frame_count = int(frame_count_float)
        duration = frame_count / frame_rate
        if timestamp >= duration:
            raise VisualBaselineError("timestamp_out_of_range")
        frame_index = int(math.floor(timestamp * frame_rate + 1e-9))
        if frame_index < 0 or frame_index >= frame_count:
            raise VisualBaselineError("timestamp_out_of_range")
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = capture.read()
        if not ok or frame is None or getattr(frame, "size", 0) == 0:
            raise VisualBaselineError("frame_decode_failed")
        encoded = _png_bytes(frame)
        _assert_output_not_source(output, source, roots)
        _write_bytes(output, encoded)
        record = {
            "schema_version": SCHEMA_VERSION,
            "record_kind": "private_visual_frame",
            "source_logical_id": item["logical_id"],
            "source_root_alias": item["root_alias"],
            "source_category": item["category"],
            "requested_timestamp_seconds": round(timestamp, 6),
            "actual_timestamp_seconds": round(frame_index / frame_rate, 6),
            "frame_index": frame_index,
            "frame_rate": round(frame_rate, 6),
            "frame_count": frame_count,
            "duration_seconds": round(duration, 6),
            "width": int(frame.shape[1]),
            "height": int(frame.shape[0]),
            "channels": _shape(frame)[2],
            "alpha_present": False,
            "png_sha256": _sha256_bytes(encoded),
            "png_bytes": len(encoded),
            "output_file": output.name,
            "status": "EXTRACTED",
        }
        _write_json(metadata_output, record)
        return record
    finally:
        capture.release()


def extract_frame(
    manifest: Mapping[str, Any],
    roots: Mapping[str, Path],
    logical_id: str,
    timestamp: float,
    output: Path,
    metadata_output: Path,
) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[1]
    output = _private_output(output, repo_root)
    metadata_output = _private_output(metadata_output, repo_root)
    item = _item_by_logical_id(manifest, logical_id)
    source = _source_path_for_item(item, roots)
    category = item.get("category")
    if output.suffix.lower() != ".png":
        raise VisualBaselineError("output_extension")
    if metadata_output == output:
        raise VisualBaselineError("output_collision")
    if category == "image":
        return _extract_image_frame(item, source, timestamp, output, metadata_output, roots)
    if category == "video":
        return _extract_video_frame(item, source, timestamp, output, metadata_output, roots)
    raise VisualBaselineError("asset_not_visual")


def _parse_shot(value: str) -> tuple[str, str, float]:
    """Parse ``state_id=logical_id@timestamp`` without accepting a path."""

    if not isinstance(value, str) or "=" not in value or "@" not in value:
        raise VisualBaselineError("shot_spec")
    state_id, remainder = value.split("=", 1)
    logical_id, raw_timestamp = remainder.rsplit("@", 1)
    if not STATE_ID_RE.fullmatch(state_id) or state_id not in EXPECTED_STATE_IDS:
        raise VisualBaselineError("shot_state_invalid")
    if not LOGICAL_ID_RE.fullmatch(logical_id):
        raise VisualBaselineError("shot_logical_id_invalid")
    return state_id, logical_id, _parse_timestamp(raw_timestamp)


def _frame_output_paths(output_dir: Path, index: int) -> tuple[Path, Path]:
    stem = f"frame_{index:04d}"
    return output_dir / f"{stem}.png", output_dir / f"{stem}.json"


def create_contact_sheet(
    frame_records: Sequence[Mapping[str, Any]],
    frame_dir: Path,
    output: Path,
    metadata_output: Path,
    *,
    columns: int = 3,
) -> dict[str, Any]:
    _require_cv2()
    repo_root = Path(__file__).resolve().parents[1]
    output = _private_output(output, repo_root)
    metadata_output = _private_output(metadata_output, repo_root)
    if not frame_records:
        raise VisualBaselineError("contact_sheet_empty")
    if columns < 1:
        raise VisualBaselineError("contact_sheet_columns")
    frames: list[Any] = []
    for record in frame_records:
        name = record.get("output_file")
        if not isinstance(name, str) or Path(name).name != name or not name.lower().endswith(".png"):
            raise VisualBaselineError("frame_output_invalid")
        frame_path = frame_dir / name
        _private_evidence_read(frame_path)
        frame = cv2.imread(str(frame_path), cv2.IMREAD_UNCHANGED)
        if frame is None or getattr(frame, "size", 0) == 0:
            raise VisualBaselineError("frame_decode_failed")
        if frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        elif frame.shape[2] == 4:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        frames.append(frame)
    cell_width = 640
    cell_height = 360
    cells: list[Any] = []
    for index, frame in enumerate(frames):
        height, width = frame.shape[:2]
        scale = min(cell_width / max(width, 1), cell_height / max(height, 1))
        resized = cv2.resize(frame, (max(1, int(width * scale)), max(1, int(height * scale))))
        canvas = np.zeros((cell_height, cell_width, 3), dtype=np.uint8)
        y = (cell_height - resized.shape[0]) // 2
        x = (cell_width - resized.shape[1]) // 2
        canvas[y : y + resized.shape[0], x : x + resized.shape[1]] = resized
        label = f"frame={index + 1} t={record.get('requested_timestamp_seconds', 0):.3f}s"
        cv2.putText(canvas, label, (12, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 1, cv2.LINE_AA)
        cells.append(canvas)
    rows = math.ceil(len(cells) / columns)
    sheet = np.zeros((rows * cell_height, columns * cell_width, 3), dtype=np.uint8)
    for index, cell in enumerate(cells):
        row, column = divmod(index, columns)
        sheet[row * cell_height : (row + 1) * cell_height, column * cell_width : (column + 1) * cell_width] = cell
    encoded = _png_bytes(sheet)
    _write_bytes(output, encoded)
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_kind": CONTACT_SHEET_KIND,
        "frame_count": len(frame_records),
        "columns": columns,
        "cell_width": cell_width,
        "cell_height": cell_height,
        "width": int(sheet.shape[1]),
        "height": int(sheet.shape[0]),
        "png_sha256": _sha256_bytes(encoded),
        "png_bytes": len(encoded),
        "output_file": output.name,
        "frames": [
            {
                "source_logical_id": record.get("source_logical_id"),
                "requested_timestamp_seconds": record.get("requested_timestamp_seconds"),
                "output_file": record.get("output_file"),
            }
            for record in frame_records
        ],
    }
    _write_json(metadata_output, record)
    return record


def _private_evidence_read(path: Path) -> Path:
    """Validate a path already known to be under ``.evidence``."""

    try:
        repo_root = Path(__file__).resolve().parents[1]
        checked = asset_manifest.ensure_private_output_path(path, repo_root)
    except asset_manifest.ManifestError as exc:
        raise VisualBaselineError(exc.code) from exc
    if not checked.is_file():
        raise VisualBaselineError("evidence_input_missing")
    return checked


def frame_index_document(records: Sequence[Mapping[str, Any]], contact_sheet: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_kind": FRAME_INDEX_KIND,
        "frame_count": len(records),
        "contact_sheet": dict(contact_sheet) if contact_sheet is not None else None,
        "frames": [dict(record) for record in records],
    }


def _contains(text: str, *terms: str) -> bool:
    lowered = text.casefold()
    return any(term.casefold() in lowered for term in terms)


def _items_for_state(manifest: Mapping[str, Any], state_id: str) -> list[Mapping[str, Any]]:
    items = [item for item in manifest.get("items", []) if isinstance(item, dict)]
    visual = [item for item in items if item.get("category") in {"image", "video"}]

    def path(item: Mapping[str, Any]) -> str:
        return str(item.get("relative_path", ""))

    def prefer(item: Mapping[str, Any]) -> tuple[int, str, str]:
        alias_order = {"game": 0, "olivia": 1, "unpacked": 2, "player": 3}
        category_order = {"video": 0, "image": 1}
        return (
            alias_order.get(str(item.get("root_alias")), 9),
            category_order.get(str(item.get("category")), 9),
            path(item).casefold(),
        )

    selected: list[Mapping[str, Any]] = []
    if state_id in {"day", "dusk", "night"}:
        token = {"day": "1200", "dusk": "1730", "night": "2000"}[state_id]
        selected = [item for item in visual if token in path(item) and "transition" not in path(item).casefold()]
    elif state_id == "idle":
        selected = [item for item in visual if _contains(path(item), "idle")]
    elif state_id == "piano_performance":
        selected = [item for item in visual if _contains(path(item), "midi", "演奏", "performance", "piano")]
    elif state_id == "letter_reply":
        selected = [
            item
            for item in visual
            if _contains(path(item), "postcard_user", "postcard_retry", "postcard_sealed", "review_failed", "mail_stamp")
        ]
    elif state_id == "letter_reading":
        selected = [item for item in visual if _contains(path(item), "postcard_letter", "postcard_video", "letter", "mailbox")]
    elif state_id == "live":
        selected = [item for item in visual if _contains(path(item), "mode-interactive", "mode-focus", "live", "dialog")]
    elif state_id == "outfit_variants":
        selected = [
            item
            for item in visual
            if re.search(r"(?:^|/)[ABC]_R[123]_(?:1200|1730|2000)\.mp4$", path(item), re.IGNORECASE)
            and str(item.get("root_alias")) == "game"
        ]
        selected.sort(key=lambda item: (re.search(r"_R([123])_", path(item), re.IGNORECASE).group(1), path(item)) if re.search(r"_R([123])_", path(item), re.IGNORECASE) else ("9", path(item)))
    elif state_id == "scene_transitions":
        selected = [item for item in visual if _contains(path(item), "transition")]
    if state_id == "letter_reply":
        selected.sort(
            key=lambda item: (
                0 if _contains(path(item), "postcard_user") else 1 if _contains(path(item), "postcard_retry", "postcard_sealed", "review_failed") else 2,
                0 if str(item.get("root_alias")) == "unpacked" else 1,
                path(item).casefold(),
            )
        )
    elif state_id == "letter_reading":
        selected.sort(
            key=lambda item: (
                0 if _contains(path(item), "postcard_letter") else 1 if _contains(path(item), "postcard_video") else 2,
                0 if str(item.get("root_alias")) == "unpacked" else 1,
                path(item).casefold(),
            )
        )
    if state_id not in {"letter_reply", "letter_reading"}:
        selected.sort(key=prefer)
    if state_id in {"day", "dusk", "night", "idle", "letter_reply", "letter_reading", "live", "scene_transitions"}:
        return selected[:1]
    if state_id == "piano_performance":
        return selected[:1]
    if state_id == "outfit_variants":
        chosen: list[Mapping[str, Any]] = []
        seen_variants: set[str] = set()
        for item in selected:
            match = re.search(r"_R([123])_", path(item), re.IGNORECASE)
            variant = match.group(1) if match else "?"
            if variant not in seen_variants:
                chosen.append(item)
                seen_variants.add(variant)
        return chosen[:3]
    return selected[:1]


def build_state_candidates(manifest: Mapping[str, Any]) -> dict[str, Any]:
    units: list[dict[str, Any]] = []
    for state_id in EXPECTED_STATE_IDS:
        selected = _items_for_state(manifest, state_id)
        ids = [str(item["logical_id"]) for item in selected]
        if ids:
            units.append(
                {
                    "state_id": state_id,
                    "status": "CANDIDATE",
                    "evidence_count": 0,
                    "required_shots": 3 if state_id == "outfit_variants" else 1,
                    "notes_code": "PATH_NAME_ONLY",
                    "inference_basis": ["manifest_logical_id", "path_name_heuristic"],
                    "candidate_asset_ids": ids,
                    "evidence_frame_ids": [],
                }
            )
        else:
            units.append(
                {
                    "state_id": state_id,
                    "status": "BLOCKED",
                    "evidence_count": 0,
                    "required_shots": 3 if state_id == "outfit_variants" else 1,
                    "notes_code": "NO_REGISTERED_MEDIA",
                    "inference_basis": ["manifest_logical_id"],
                    "candidate_asset_ids": [],
                    "evidence_frame_ids": [],
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "matrix_kind": MATRIX_KIND,
        "source_selection": "B01A_LOGICAL_IDS_ONLY",
        "verification_boundary": {
            "path_name_inference_can_verify": False,
            "thresholds": "UNFROZEN",
            "candidate_media_generated": False,
        },
        "state_units": units,
    }


def apply_frame_evidence(
    candidates: Mapping[str, Any],
    frame_records: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    document = json.loads(json.dumps(candidates))
    by_state: dict[str, list[str]] = {state_id: [] for state_id in EXPECTED_STATE_IDS}
    for record in frame_records:
        state_id = record.get("state_id")
        frame_id = record.get("frame_id") or record.get("output_file")
        if isinstance(state_id, str) and state_id in by_state and isinstance(frame_id, str):
            by_state[state_id].append(frame_id)
    for unit in document.get("state_units", []):
        state_id = unit.get("state_id")
        if state_id not in by_state:
            continue
        frame_ids = by_state[state_id]
        unit["evidence_frame_ids"] = frame_ids
        unit["evidence_count"] = len(frame_ids)
        if frame_ids and unit.get("status") != "BLOCKED":
            unit["status"] = "UNVERIFIED"
            unit["notes_code"] = "EXTRACTED_MANUAL_REVIEW_REQUIRED"
    return document


def validate_state_matrix_document(document: Any) -> tuple[str, ...]:
    issues: list[str] = []
    if not isinstance(document, dict):
        return ("schema",)
    if document.get("schema_version") != SCHEMA_VERSION or document.get("matrix_kind") != MATRIX_KIND:
        issues.append("schema")
    boundary = document.get("verification_boundary")
    if not isinstance(boundary, dict):
        issues.append("schema")
    else:
        if boundary.get("path_name_inference_can_verify") is not False:
            issues.append("verification_boundary")
        if boundary.get("thresholds") != "UNFROZEN":
            issues.append("thresholds_unfrozen")
    raw_units = document.get("state_units")
    if not isinstance(raw_units, list):
        return tuple(issues + ["schema"])
    found: set[str] = set()
    for unit in raw_units:
        if not isinstance(unit, dict):
            issues.append("schema")
            continue
        required = {"state_id", "status", "evidence_count", "required_shots", "notes_code"}
        if not required.issubset(unit):
            issues.append("schema")
            continue
        state_id = unit.get("state_id")
        status = unit.get("status")
        if not isinstance(state_id, str) or not STATE_ID_RE.fullmatch(state_id) or state_id in found:
            issues.append("state_id")
        else:
            found.add(state_id)
        if status not in STATUS_VALUES:
            issues.append("status")
        evidence_count = unit.get("evidence_count")
        required_shots = unit.get("required_shots")
        if not isinstance(evidence_count, int) or isinstance(evidence_count, bool) or evidence_count < 0:
            issues.append("evidence_count")
        if not isinstance(required_shots, int) or isinstance(required_shots, bool) or required_shots < 1:
            issues.append("required_shots")
        notes_code = unit.get("notes_code")
        if not isinstance(notes_code, str) or not NOTES_CODE_RE.fullmatch(notes_code):
            issues.append("notes_code")
        ids = unit.get("candidate_asset_ids", [])
        if not isinstance(ids, list) or any(not isinstance(value, str) or not LOGICAL_ID_RE.fullmatch(value) for value in ids):
            issues.append("logical_id")
        frame_ids = unit.get("evidence_frame_ids", [])
        if not isinstance(frame_ids, list) or any(not isinstance(value, str) or not value for value in frame_ids):
            issues.append("evidence_frame_ids")
        if status == "VERIFIED":
            if unit.get("verification_method") != "manual_review":
                issues.append("verified_requires_manual_review")
            if not isinstance(evidence_count, int) or not isinstance(required_shots, int) or evidence_count < required_shots:
                issues.append("verified_requires_evidence")
            if notes_code == "PATH_NAME_ONLY":
                issues.append("verified_from_path_name")
        if status == "CANDIDATE" and unit.get("verification_method") == "manual_review":
            issues.append("candidate_review_mismatch")
    if found != set(EXPECTED_STATE_IDS):
        issues.append("state_coverage")
    return tuple(issues)


def synthetic_state_matrix_document() -> dict[str, Any]:
    units = []
    for state_id in EXPECTED_STATE_IDS:
        units.append(
            {
                "state_id": state_id,
                "status": "UNVERIFIED",
                "evidence_count": 0,
                "required_shots": 3 if state_id == "outfit_variants" else 1,
                "notes_code": "SYNTHETIC_UNVERIFIED",
                "candidate_asset_ids": [],
                "evidence_frame_ids": [],
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "matrix_kind": MATRIX_KIND,
        "source_selection": "SYNTHETIC_EXAMPLE",
        "verification_boundary": {
            "path_name_inference_can_verify": False,
            "thresholds": "UNFROZEN",
            "candidate_media_generated": False,
        },
        "state_units": units,
    }


def state_matrix_schema_document() -> dict[str, Any]:
    unit_properties: dict[str, Any] = {
        "state_id": {"type": "string", "pattern": r"^[a-z][a-z0-9_]+$"},
        "status": {"enum": sorted(STATUS_VALUES)},
        "evidence_count": {"type": "integer", "minimum": 0},
        "required_shots": {"type": "integer", "minimum": 1},
        "notes_code": {"type": "string", "pattern": r"^[A-Z0-9_]+$"},
        "inference_basis": {"type": "array", "items": {"type": "string"}},
        "candidate_asset_ids": {
            "type": "array",
            "items": {"type": "string", "pattern": r"^asset_[0-9a-f]{32}$"},
        },
        "evidence_frame_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "verification_method": {"enum": ["manual_review"]},
    }
    unit = {
        "type": "object",
        "additionalProperties": False,
        "required": ["state_id", "status", "evidence_count", "required_shots", "notes_code"],
        "properties": unit_properties,
    }
    return {
        "$schema": "https://example.invalid/endpoint",
        "$id": "visual_state_matrix.schema.json",
        "title": "Private visual baseline state matrix",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "matrix_kind", "source_selection", "verification_boundary", "state_units"],
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "matrix_kind": {"const": MATRIX_KIND},
            "source_selection": {"type": "string"},
            "verification_boundary": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path_name_inference_can_verify", "thresholds", "candidate_media_generated"],
                "properties": {
                    "path_name_inference_can_verify": {"const": False},
                    "thresholds": {"const": "UNFROZEN"},
                    "candidate_media_generated": {"const": False},
                },
            },
            "state_units": {
                "type": "array",
                "minItems": len(EXPECTED_STATE_IDS),
                "items": unit,
            },
        },
        "allOf": [
            {
                "contains": {
                    "type": "object",
                    "required": ["state_id"],
                    "properties": {"state_id": {"const": state_id}},
                },
                "minContains": 1,
            }
            for state_id in EXPECTED_STATE_IDS
        ],
    }


def sanitized_summary_document(
    manifest: Mapping[str, Any],
    state_matrix: Mapping[str, Any],
    *,
    frame_count: int,
    contact_sheet_count: int,
    metric_status_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    root_counts = {str(root.get("alias")): int(root.get("item_count", 0)) for root in manifest.get("roots", []) if isinstance(root, dict)}
    category_counts = Counter(str(item.get("category")) for item in manifest.get("items", []) if isinstance(item, dict))
    units = state_matrix.get("state_units", [])
    coverage = [
        {
            "state_id": unit.get("state_id"),
            "status": unit.get("status"),
            "evidence_count": unit.get("evidence_count"),
            "required_shots": unit.get("required_shots"),
            "notes_code": unit.get("notes_code"),
        }
        for unit in units
        if isinstance(unit, dict)
    ]
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "summary_kind": "visual_baseline_sanitized_summary",
        "registered_source_roots": root_counts,
        "registered_media_counts": dict(sorted(category_counts.items())),
        "private_evidence": {
            "frame_count": int(frame_count),
            "contact_sheet_count": int(contact_sheet_count),
            "contains_candidate_media": False,
        },
        "state_coverage": coverage,
        "verification_boundary": {
            "thresholds": "UNFROZEN",
            "automatic_metrics_do_not_replace_total_control_viewing": True,
            "identity_provider": "REPLACEABLE_HOOK_ONLY",
            "state_filename_inference_can_be_verified": False,
        },
    }
    if metric_status_counts is not None:
        result["metric_status_counts"] = dict(sorted((str(k), int(v)) for k, v in metric_status_counts.items()))
    return result


def dependency_report() -> dict[str, Any]:
    """Report optional capabilities without probing or downloading anything."""

    try:
        import importlib.util

        skimage_available = importlib.util.find_spec("skimage") is not None
        lpips_available = importlib.util.find_spec("lpips") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        skimage_available = False
        lpips_available = False
    return {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "private_visual_dependency_report",
        "opencv": {
            "available": cv2 is not None,
            "version": getattr(cv2, "__version__", None) if cv2 is not None else None,
            "video_decode_capability": "available" if cv2 is not None else "unavailable",
        },
        "numpy": {"available": np is not None, "version": getattr(np, "__version__", None) if np is not None else None},
        "ffmpeg": {"available": shutil.which("ffmpeg") is not None, "diagnostic": "not_on_path" if shutil.which("ffmpeg") is None else "available"},
        "ffprobe": {"available": shutil.which("ffprobe") is not None, "diagnostic": "not_on_path" if shutil.which("ffprobe") is None else "available"},
        "ssim": {"available": skimage_available, "diagnostic": "dependency_missing:skimage" if not skimage_available else "available"},
        "lpips": {"available": lpips_available, "diagnostic": "provider_not_configured" if not lpips_available else "provider_hook_required"},
        "identity": {"status": "UNVERIFIED", "diagnostic": "manual_identity_review_required", "provider": "REPLACEABLE_HOOK_ONLY"},
        "thresholds": "UNFROZEN",
    }


def _frame_id_for(index: int, state_id: str) -> str:
    return f"{state_id}_shot_{index:03d}"


def build_evidence_package(input_dir: Path, package_output: Path, manifest_output: Path) -> dict[str, Any]:
    """Create a UTF-8 inventory and a detached SHA-256 manifest.

    The manifest intentionally excludes itself and the package inventory so
    its digest is stable and can be checked independently.  All listed paths
    are relative to the private evidence run and never absolute source paths.
    """

    try:
        resolved_input = input_dir.resolve(strict=True)
        resolved_package = package_output.resolve(strict=False)
        resolved_manifest = manifest_output.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise VisualBaselineError("evidence_boundary") from exc
    if not resolved_input.is_dir() or not _is_within(resolved_package, resolved_input) or not _is_within(resolved_manifest, resolved_input):
        raise VisualBaselineError("evidence_boundary")
    excluded = {resolved_package, resolved_manifest}
    files: list[dict[str, Any]] = []
    for path in sorted(resolved_input.rglob("*"), key=lambda value: value.relative_to(resolved_input).as_posix()):
        if not path.is_file() or path.resolve(strict=False) in excluded:
            continue
        relative = path.relative_to(resolved_input).as_posix()
        files.append(
            {
                "relative_file": relative,
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    manifest_text = "".join(f"{entry['sha256']}  {entry['relative_file']}\n" for entry in files)
    _write_bytes(manifest_output, manifest_text.encode("utf-8"), overwrite=True)
    manifest_sha256 = _sha256_file(manifest_output)
    package = {
        "schema_version": SCHEMA_VERSION,
        "package_kind": "private_visual_baseline_evidence",
        "encoding": "UTF-8",
        "contains_original_media": True,
        "candidate_media_generated": False,
        "manifest_file": manifest_output.name,
        "manifest_sha256": manifest_sha256,
        "files": files,
    }
    _write_json(package_output, package)
    package["package_sha256"] = _sha256_file(package_output)
    return package


def _cmd_candidates(args: argparse.Namespace) -> int:
    repo_root = asset_manifest._repo_root(args.repo_root)
    manifest, _ = _load_registered_manifest(args.manifest, args.root, repo_root)
    output = _private_output(args.output, repo_root)
    document = build_state_candidates(manifest)
    _write_json(output, document)
    print("status=PASS")
    print(f"states={len(document['state_units'])}")
    print(f"candidate_media_generated={int(document['verification_boundary']['candidate_media_generated'])}")
    print("private_output_written=1")
    return 0


def _cmd_extract_batch(args: argparse.Namespace) -> int:
    repo_root = asset_manifest._repo_root(args.repo_root)
    manifest, roots = _load_registered_manifest(args.manifest, args.root, repo_root)
    output_dir = _private_output(args.output_dir, repo_root)
    index_output = _private_output(args.frame_index, repo_root)
    if output_dir == index_output or _is_within(index_output, output_dir) or _is_within(output_dir, index_output):
        raise VisualBaselineError("output_collision")
    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for index, raw_shot in enumerate(args.shot, start=1):
        state_id, logical_id, timestamp = _parse_shot(raw_shot)
        frame_output, metadata_output = _frame_output_paths(output_dir, index)
        record = extract_frame(manifest, roots, logical_id, timestamp, frame_output, metadata_output)
        record["state_id"] = state_id
        record["frame_id"] = _frame_id_for(index, state_id)
        _write_json(metadata_output, record)
        records.append(record)
    contact_sheet_record: dict[str, Any] | None = None
    if args.contact_sheet:
        contact_sheet = _private_output(args.contact_sheet, repo_root)
        contact_metadata = _private_output(args.contact_sheet_metadata or str(contact_sheet.with_suffix(".json")), repo_root)
        contact_sheet_record = create_contact_sheet(records, output_dir, contact_sheet, contact_metadata, columns=args.columns)
    _write_json(index_output, frame_index_document(records, contact_sheet_record))
    print("status=PASS")
    print(f"frames_extracted={len(records)}")
    print(f"contact_sheets={int(contact_sheet_record is not None)}")
    print("private_output_written=1")
    return 0


def _cmd_matrix(args: argparse.Namespace) -> int:
    repo_root = asset_manifest._repo_root(args.repo_root)
    manifest, _ = _load_registered_manifest(args.manifest, args.root, repo_root)
    output = _private_output(args.output, repo_root)
    candidates = build_state_candidates(manifest)
    frame_records: list[Mapping[str, Any]] = []
    if args.frame_index:
        frame_index_path = _private_input(args.frame_index, repo_root)
        frame_index = _read_json(frame_index_path)
        if not isinstance(frame_index, dict) or frame_index.get("record_kind") != FRAME_INDEX_KIND:
            raise VisualBaselineError("frame_index_invalid")
        raw_frames = frame_index.get("frames")
        if not isinstance(raw_frames, list):
            raise VisualBaselineError("frame_index_invalid")
        frame_records = [record for record in raw_frames if isinstance(record, dict)]
    document = apply_frame_evidence(candidates, frame_records)
    issues = validate_state_matrix_document(document)
    if issues:
        raise VisualBaselineError("state_matrix_invalid")
    _write_json(output, document)
    if args.summary:
        summary_output = asset_manifest.ensure_repo_output_path(args.summary, repo_root)
        _write_json(summary_output, sanitized_summary_document(manifest, document, frame_count=len(frame_records), contact_sheet_count=int(bool(frame_records))))
    print("status=PASS")
    print(f"states={len(document['state_units'])}")
    print(f"states_with_evidence={sum(1 for unit in document['state_units'] if unit['evidence_count'])}")
    print("private_output_written=1")
    return 0


def _cmd_package(args: argparse.Namespace) -> int:
    repo_root = asset_manifest._repo_root(args.repo_root)
    input_dir = _private_output(args.input_dir, repo_root)
    package_output = _private_output(args.output, repo_root)
    manifest_output = _private_output(args.manifest_output, repo_root)
    package = build_evidence_package(input_dir, package_output, manifest_output)
    print("status=PASS")
    print(f"files={len(package['files'])}")
    print(f"manifest_sha256={package['manifest_sha256']}")
    print(f"package_sha256={package['package_sha256']}")
    print("private_output_written=1")
    return 0


def _cmd_diagnostics(args: argparse.Namespace) -> int:
    repo_root = asset_manifest._repo_root(args.repo_root)
    output = _private_output(args.output, repo_root)
    _write_json(output, dependency_report())
    print("status=PASS")
    print("private_output_written=1")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    candidates = subparsers.add_parser("candidates", help="build conservative state candidates from a private manifest")
    candidates.add_argument("--manifest", required=True)
    candidates.add_argument("--root", action="append", required=True, metavar="ALIAS=PATH")
    candidates.add_argument("--output", required=True)
    candidates.set_defaults(handler=_cmd_candidates)

    extract = subparsers.add_parser("extract-batch", help="extract explicit timestamp shots by logical ID")
    extract.add_argument("--manifest", required=True)
    extract.add_argument("--root", action="append", required=True, metavar="ALIAS=PATH")
    extract.add_argument("--shot", action="append", required=True, metavar="STATE=LOGICAL_ID@SECONDS")
    extract.add_argument("--output-dir", required=True)
    extract.add_argument("--frame-index", required=True)
    extract.add_argument("--contact-sheet")
    extract.add_argument("--contact-sheet-metadata")
    extract.add_argument("--columns", type=int, default=3)
    extract.set_defaults(handler=_cmd_extract_batch)

    matrix = subparsers.add_parser("matrix", help="write a state matrix and optional sanitized summary")
    matrix.add_argument("--manifest", required=True)
    matrix.add_argument("--root", action="append", required=True, metavar="ALIAS=PATH")
    matrix.add_argument("--frame-index")
    matrix.add_argument("--output", required=True)
    matrix.add_argument("--summary")
    matrix.set_defaults(handler=_cmd_matrix)

    package = subparsers.add_parser("package", help="write a UTF-8 evidence inventory and detached SHA-256 manifest")
    package.add_argument("--input-dir", required=True)
    package.add_argument("--output", required=True)
    package.add_argument("--manifest-output", required=True)
    package.set_defaults(handler=_cmd_package)

    diagnostics = subparsers.add_parser("diagnostics", help="write dependency and capability diagnostics")
    diagnostics.add_argument("--output", required=True)
    diagnostics.set_defaults(handler=_cmd_diagnostics)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (asset_manifest.ManifestError, VisualBaselineError) as exc:
        code = exc.code if isinstance(exc, (asset_manifest.ManifestError, VisualBaselineError)) else "error"
        print(f"status=ERROR:{code}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
