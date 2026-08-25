"""One-case synthetic tracer for the private-memory isolation experiment.

This module deliberately exposes only ``run_case01``.  It builds a fresh
memory namespace from train originals, generates one reply from the first test
original, performs the blind persona assessment, and only then opens the held-
out reference for comparison.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping


def _original_text(manifest_root: Path, item: Mapping[str, Any]) -> str:
    original = item["original"]
    return (manifest_root / original["relative_path"]).read_text(encoding="utf-8")


def _reference_text(manifest_root: Path, item: Mapping[str, Any]) -> str:
    reference = item["reference"]
    return (manifest_root / reference["relative_path"]).read_text(encoding="utf-8")


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _persona_summary(result: Mapping[str, object]) -> dict[str, object]:
    score = result.get("score")
    violations = result.get("hard_violations")
    if not _is_number(score) or not isinstance(violations, list) or not all(
        isinstance(violation, str) for violation in violations
    ):
        raise ValueError("CASE01_PERSONA_EVALUATION_INVALID")

    metrics: dict[str, int | float] = {}
    for key, value in result.items():
        if key in {"score", "hard_violations"}:
            continue
        if not isinstance(key, str) or not _is_number(value):
            raise ValueError("CASE01_PERSONA_EVALUATION_INVALID")
        metrics[key] = value
    return {
        "score": score,
        "hard_violation_count": len(violations),
        "metrics": metrics,
    }


def _reference_summary(result: Mapping[str, object]) -> dict[str, int | float]:
    if not result:
        raise ValueError("CASE01_REFERENCE_EVALUATION_INVALID")
    summary: dict[str, int | float] = {}
    for key, value in result.items():
        if not isinstance(key, str) or not _is_number(value):
            raise ValueError("CASE01_REFERENCE_EVALUATION_INVALID")
        summary[key] = value
    return summary


def _write_report(output_path: Path, report: Mapping[str, object]) -> None:
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ),
        encoding="utf-8",
    )


def run_case01(
    *,
    manifest_path: Path,
    namespace: str,
    memory_factory: Callable[[str], Any],
    generator: Callable[..., str],
    persona_evaluator: Callable[..., Mapping[str, object]],
    reference_evaluator: Callable[..., Mapping[str, object]],
    persona_authority: object,
    output_path: Path,
    validation_mode: str,
) -> dict[str, object]:
    """Run the case01 tracer while keeping held-out reference text out of generation."""

    if validation_mode != "synthetic_validation":
        raise ValueError("CASE01_VALIDATION_MODE_INVALID")

    manifest_file = Path(manifest_path)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    items = manifest["items"]
    train_items = sorted(
        (item for item in items if item["split"] == "train"),
        key=lambda item: item["global_sequence"],
    )
    if len(train_items) != 60:
        raise ValueError("CASE01_TRAIN_COUNT_INVALID")
    case01 = min(
        (item for item in items if item["split"] == "test"),
        key=lambda item: item["split_sequence"],
    )

    manifest_root = manifest_file.parent
    report_base: dict[str, object] = {
        "case_id": case01["case_id"],
        "prefix_case": "case01",
        "validation_mode": validation_mode,
        "namespace": namespace,
        "train_original_count": len(train_items),
        "test_original_count": 1,
        "private_world_arm": "fixed_disabled",
    }
    stage = "memory"
    try:
        memory = memory_factory(namespace)
        for item in train_items:
            memory.ingest_user_evidence(
                source_id=f"train:{item['case_id']}",
                text=_original_text(manifest_root, item),
            )

        case01_original = _original_text(manifest_root, case01)
        memory.ingest_user_evidence(source_id=f"test:{case01['case_id']}", text=case01_original)
        selected_evidence = memory.selected_evidence(original=case01_original)
        stage = "generator"
        reply = generator(
            persona_authority=persona_authority,
            selected_evidence=selected_evidence,
            original=case01_original,
        )
        if not isinstance(reply, str):
            raise ValueError("CASE01_GENERATOR_REPLY_INVALID")

        stage = "persona"
        persona_result = _persona_summary(persona_evaluator(reply=reply))
        stage = "reference"
        reference_result = _reference_summary(
            reference_evaluator(
                reply=reply,
                reference_text=_reference_text(manifest_root, case01),
            )
        )
    except Exception:
        error_code = {
            "memory": "CASE01_MEMORY_UNAVAILABLE",
            "generator": "CASE01_GENERATOR_UNAVAILABLE",
            "persona": "CASE01_PERSONA_EVALUATION_UNAVAILABLE",
            "reference": "CASE01_REFERENCE_EVALUATION_UNAVAILABLE",
        }[stage]
        _write_report(
            output_path,
            {**report_base, "status": "unavailable", "error_code": error_code},
        )
        raise

    report: dict[str, object] = {
        **report_base,
        "status": "completed",
        "error_code": None,
        "selected_evidence_count": len(selected_evidence),
        "persona_evaluation": persona_result,
        "reference_evaluation": reference_result,
    }
    _write_report(output_path, report)
    return report
