"""Synthetic tracers for isolated private-memory prefix experiments."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping


_PERSONA_METRICS = frozenset(
    {
        "identity",
        "tone",
        "reply_rhythm",
        "boundaries",
        "continuity",
        "non_overintimacy",
        "no_invention",
        "style_stability",
    }
)
_REFERENCE_METRICS = frozenset({"style_score", "focus_score"})


def _original_text(manifest_root: Path, item: Mapping[str, Any]) -> str:
    original = item["original"]
    return (manifest_root / original["relative_path"]).read_text(encoding="utf-8")


def _reference_text(manifest_root: Path, item: Mapping[str, Any]) -> str:
    reference = item["reference"]
    return (manifest_root / reference["relative_path"]).read_text(encoding="utf-8")


def _is_score(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0 <= float(value) <= 1
    )


def _persona_summary(result: Mapping[str, object]) -> dict[str, object]:
    score = result.get("score")
    violations = result.get("hard_violations")
    if not set(result).issubset({"score", "hard_violations", *_PERSONA_METRICS}) or not _is_score(
        score
    ) or not isinstance(violations, list) or not all(
        isinstance(violation, str) for violation in violations
    ):
        raise ValueError("CASE01_PERSONA_EVALUATION_INVALID")

    metrics: dict[str, int | float] = {}
    for key, value in result.items():
        if key in {"score", "hard_violations"}:
            continue
        if not isinstance(key, str) or not _is_score(value):
            raise ValueError("CASE01_PERSONA_EVALUATION_INVALID")
        metrics[key] = value
    return {
        "score": score,
        "hard_violation_count": len(violations),
        "metrics": metrics,
    }


def _reference_summary(result: Mapping[str, object]) -> dict[str, int | float]:
    if not result or not set(result).issubset(_REFERENCE_METRICS):
        raise ValueError("CASE01_REFERENCE_EVALUATION_INVALID")
    summary: dict[str, int | float] = {}
    for key, value in result.items():
        if not isinstance(key, str) or not _is_score(value):
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
        persona_result = _persona_summary(
            persona_evaluator(
                reply=reply,
                original=case01_original,
                selected_evidence=selected_evidence,
            )
        )
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


def run_prefix19(
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
    """Run the 19 fresh-memory prefixes without exposing held-out media."""

    if validation_mode not in {
        "synthetic_validation",
        "private_local_validation",
    }:
        raise ValueError("PREFIX19_VALIDATION_MODE_INVALID")

    manifest_file = Path(manifest_path)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    items = manifest["items"]
    ordered = lambda item: (item["split_sequence"], item["source_date"], item["source_order"])
    train_items = sorted((item for item in items if item["split"] == "train"), key=ordered)
    test_items = sorted((item for item in items if item["split"] == "test"), key=ordered)
    if len(train_items) != 60 or len(test_items) != 19:
        raise ValueError("PREFIX19_SPLIT_COUNTS_INVALID")
    reference_kinds = [item["reference"]["kind"] for item in test_items]
    if reference_kinds.count("text") != 17 or reference_kinds.count("video") != 2:
        raise ValueError("PREFIX19_REFERENCE_KINDS_INVALID")

    manifest_root = manifest_file.parent
    reports: list[dict[str, object]] = []
    for prefix_count, target in enumerate(test_items, start=1):
        case_namespace = f"{namespace}:case{prefix_count:02d}"
        report_base: dict[str, object] = {
            "case_id": target["case_id"],
            "prefix_case": f"case{prefix_count:02d}",
            "namespace": case_namespace,
            "train_original_count": len(train_items),
            "test_original_count": prefix_count,
            "private_world_arm": "fixed_disabled",
        }
        stage = "memory"
        try:
            memory = memory_factory(case_namespace)
            for item in train_items:
                memory.ingest_user_evidence(
                    source_id=f"train:{item['case_id']}",
                    text=_original_text(manifest_root, item),
                )
            prefix = test_items[:prefix_count]
            for item in prefix:
                memory.ingest_user_evidence(
                    source_id=f"test:{item['case_id']}",
                    text=_original_text(manifest_root, item),
                )

            original = _original_text(manifest_root, target)
            selected_evidence = memory.selected_evidence(original=original)
            stage = "generator"
            reply = generator(
                persona_authority=persona_authority,
                selected_evidence=selected_evidence,
                original=original,
            )
            if not isinstance(reply, str):
                raise ValueError("PREFIX19_GENERATOR_REPLY_INVALID")

            stage = "persona"
            persona_result = _persona_summary(
                persona_evaluator(
                    reply=reply,
                    original=original,
                    selected_evidence=selected_evidence,
                )
            )
            stage = "reference"
            if target["reference"]["kind"] == "text":
                reference_result = _reference_summary(
                    reference_evaluator(
                        reply=reply,
                        reference_text=_reference_text(manifest_root, target),
                    )
                )
                reference_status = "evaluated_text"
            else:
                reference_result = None
                reference_status = "not_evaluated_media"
        except Exception:
            error_code = {
                "memory": "PREFIX19_MEMORY_UNAVAILABLE",
                "generator": "PREFIX19_GENERATOR_UNAVAILABLE",
                "persona": "PREFIX19_PERSONA_EVALUATION_UNAVAILABLE",
                "reference": "PREFIX19_REFERENCE_EVALUATION_UNAVAILABLE",
            }[stage]
            failed_case = {
                key: report_base[key]
                for key in {
                    "case_id",
                    "prefix_case",
                    "namespace",
                    "private_world_arm",
                }
            }
            failed_case.update(status="unavailable", error_code=error_code)
            _write_report(
                output_path,
                {
                    "status": "unavailable",
                    "validation_mode": validation_mode,
                    "private_world_arm": "fixed_disabled",
                    "failed_case": failed_case,
                },
            )
            raise

        report: dict[str, object] = {
            **report_base,
            "status": "completed",
            "error_code": None,
            "selected_evidence_count": len(selected_evidence),
            "persona_evaluation": persona_result,
            "reference_status": reference_status,
        }
        if reference_result is not None:
            report["reference_evaluation"] = reference_result
        reports.append(report)

    result: dict[str, object] = {
        "status": "completed",
        "error_code": None,
        "validation_mode": validation_mode,
        "private_world_arm": "fixed_disabled",
        "completed_case_count": len(reports),
        "cases": reports,
    }
    _write_report(output_path, result)
    return result
