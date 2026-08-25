"""One-case synthetic tracer for the private-memory isolation experiment.

This module deliberately exposes only ``run_case01``.  It builds a fresh
memory namespace from train originals, generates one reply from the first test
original, performs the blind persona assessment, and only then opens the held-
out reference for comparison.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping


def _original_text(manifest_root: Path, item: Mapping[str, Any]) -> str:
    original = item["original"]
    return (manifest_root / original["relative_path"]).read_text(encoding="utf-8")


def _reference_text(manifest_root: Path, item: Mapping[str, Any]) -> str:
    reference = item["reference"]
    return (manifest_root / reference["relative_path"]).read_text(encoding="utf-8")


def _result_summary(result: Mapping[str, object], *, include_hard_violations: bool) -> dict[str, object]:
    summary: dict[str, object] = {
        key: value
        for key, value in result.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    if include_hard_violations:
        violations = result.get("hard_violations", [])
        summary["hard_violation_count"] = len(violations) if isinstance(violations, list) else 0
    return summary


def run_case01(
    *,
    manifest_path: Path,
    memory_factory: Callable[[str], Any],
    generator: Callable[..., str],
    persona_evaluator: Callable[..., Mapping[str, object]],
    reference_evaluator: Callable[..., Mapping[str, object]],
    persona_authority: object,
    output_path: Path,
    validation_mode: str,
) -> dict[str, object]:
    """Run the case01 tracer while keeping held-out reference text out of generation."""

    manifest_file = Path(manifest_path)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    items = manifest["items"]
    train_items = sorted(
        (item for item in items if item["split"] == "train"),
        key=lambda item: item["global_sequence"],
    )
    case01 = min(
        (item for item in items if item["split"] == "test"),
        key=lambda item: item["split_sequence"],
    )

    manifest_root = manifest_file.parent
    namespace = "memory-isolation-case01"
    memory = memory_factory(namespace)
    for item in train_items:
        memory.ingest_user_evidence(
            source_id=f"train:{item['case_id']}",
            text=_original_text(manifest_root, item),
        )

    case01_original = _original_text(manifest_root, case01)
    memory.ingest_user_evidence(source_id=f"test:{case01['case_id']}", text=case01_original)
    selected_evidence = memory.selected_evidence(original=case01_original)
    reply = generator(
        persona_authority=persona_authority,
        selected_evidence=selected_evidence,
        original=case01_original,
    )
    persona_result = persona_evaluator(reply=reply)

    reference_result = reference_evaluator(
        reply=reply,
        reference_text=_reference_text(manifest_root, case01),
    )
    report: dict[str, object] = {
        "case_id": "case01",
        "validation_mode": validation_mode,
        "namespace": namespace,
        "train_original_count": len(train_items),
        "test_original_count": 1,
        "selected_evidence_count": len(selected_evidence),
        "persona_evaluation": _result_summary(persona_result, include_hard_violations=True),
        "reference_evaluation": _result_summary(reference_result, include_hard_violations=False),
        "boundary_flags": {
            "persona_before_reference": True,
            "private_world_used": False,
        },
    }
    output_file = Path(output_path)
    output_file.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return report
