"""Synthetic contract tests for the public case01 isolation tracer."""

from __future__ import annotations

import json
from pathlib import Path

from memory_isolation_case01 import run_case01


def _entry(relative_path: str, *, kind: str = "text") -> dict[str, str]:
    return {"relative_path": relative_path, "kind": kind}


def _manifest(tmp_path: Path) -> Path:
    items: list[dict[str, object]] = []
    for number in range(60, 0, -1):
        relative = f"originals/train-{number:02d}.txt"
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"synthetic train original {number}", encoding="utf-8")
        items.append(
            {
                "split": "train",
                "global_sequence": number,
                "split_sequence": number,
                "case_id": f"train-{number:02d}",
                "source_date": f"2025-01-{number:02d}",
                "source_order": number,
                "original": _entry(relative),
                "reference": _entry(f"references/train-{number:02d}.txt"),
            }
        )
    second_original = tmp_path / "originals/case02.txt"
    second_reference = tmp_path / "references/case02.txt"
    second_original.write_text("synthetic case02 original", encoding="utf-8")
    second_reference.parent.mkdir(parents=True, exist_ok=True)
    second_reference.write_text("synthetic second reference", encoding="utf-8")
    items.append(
        {
            "split": "test",
            "global_sequence": 62,
            "split_sequence": 2,
            "case_id": "test-second",
            "source_date": "2025-03-02",
            "source_order": 62,
            "original": _entry("originals/case02.txt"),
            "reference": _entry("references/case02.txt"),
        }
    )
    original = tmp_path / "originals/case01.txt"
    reference = tmp_path / "references/case01.txt"
    original.write_text("synthetic case01 original", encoding="utf-8")
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_text("synthetic held-out reference", encoding="utf-8")
    items.append(
        {
            "split": "test",
            "global_sequence": 61,
            "split_sequence": 1,
            "case_id": "test-first",
            "source_date": "2025-03-01",
            "source_order": 61,
            "original": _entry("originals/case01.txt"),
            "reference": _entry("references/case01.txt"),
        }
    )
    path = tmp_path / "split-manifest.json"
    path.write_text(json.dumps({"items": items}), encoding="utf-8")
    return path


def test_case01_orders_blind_persona_before_reference_and_hides_reference_from_generator(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Memory:
        def __init__(self) -> None:
            self.ingested: list[tuple[str, str]] = []

        def ingest_user_evidence(self, *, source_id: str, text: str) -> None:
            events.append(f"ingest:{source_id}")
            self.ingested.append((source_id, text))

        def selected_evidence(self, *, original: str) -> tuple[str, ...]:
            events.append("select-memory")
            assert original == "synthetic case01 original"
            return ("synthetic selected evidence",)

    memory: Memory | None = None

    def memory_factory(namespace: str) -> Memory:
        nonlocal memory
        events.append(f"memory-factory:{namespace}")
        memory = Memory()
        return memory

    def generator(**kwargs: object) -> str:
        events.append("generate")
        serialized = json.dumps(kwargs, ensure_ascii=False)
        assert "synthetic held-out reference" not in serialized
        assert "references/case01.txt" not in serialized
        assert "private_world" not in serialized
        assert kwargs["persona_authority"] == {"authority": "synthetic"}
        return "synthetic generated reply"

    def persona_evaluator(**kwargs: object) -> dict[str, object]:
        events.append("persona-evaluate")
        assert kwargs["reply"] == "synthetic generated reply"
        return {"score": 0.9, "hard_violations": []}

    def reference_evaluator(**kwargs: object) -> dict[str, float]:
        events.append("reference-evaluate")
        assert kwargs["reference_text"] == "synthetic held-out reference"
        return {"style_score": 0.8, "focus_score": 0.7}

    output_path = tmp_path / "local-only-report.json"
    report = run_case01(
        manifest_path=_manifest(tmp_path),
        memory_factory=memory_factory,
        generator=generator,
        persona_evaluator=persona_evaluator,
        reference_evaluator=reference_evaluator,
        persona_authority={"authority": "synthetic"},
        output_path=output_path,
        validation_mode="synthetic_validation",
    )

    assert memory is not None
    assert [source_id for source_id, _ in memory.ingested] == [
        *(f"train:train-{number:02d}" for number in range(1, 61)),
        "test:test-first",
    ]
    assert events == [
        "memory-factory:memory-isolation-case01",
        *(f"ingest:train:train-{number:02d}" for number in range(1, 61)),
        "ingest:test:test-first",
        "select-memory",
        "generate",
        "persona-evaluate",
        "reference-evaluate",
    ]
    assert report == json.loads(output_path.read_text(encoding="utf-8"))
    assert report["case_id"] == "case01"
    assert report["namespace"] == "memory-isolation-case01"
    assert report["validation_mode"] == "synthetic_validation"
    assert report["boundary_flags"]["persona_before_reference"] is True
    assert "synthetic case01 original" not in json.dumps(report)
    assert "synthetic generated reply" not in json.dumps(report)
    assert "synthetic held-out reference" not in json.dumps(report)
