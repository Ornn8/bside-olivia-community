"""Synthetic contract tests for the public case01 isolation tracer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from memory_isolation_case01 import run_case01, run_prefix19


def _entry(relative_path: str, *, kind: str = "text") -> dict[str, str]:
    return {"relative_path": relative_path, "kind": kind}


def _manifest(tmp_path: Path, *, train_count: int = 60) -> Path:
    items: list[dict[str, object]] = []
    for number in range(train_count, 0, -1):
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


def _manifest_19(tmp_path: Path) -> Path:
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
    for number in range(19, 0, -1):
        original = tmp_path / f"originals/case{number:02d}.txt"
        original.write_text(f"synthetic test original {number}", encoding="utf-8")
        kind = "video" if number in {7, 19} else "text"
        reference = tmp_path / f"references/case{number:02d}.{ 'mp4' if kind == 'video' else 'txt'}"
        if kind == "text":
            reference.parent.mkdir(parents=True, exist_ok=True)
            reference.write_text(f"synthetic reference {number}", encoding="utf-8")
        items.append(
            {
                "split": "test",
                "global_sequence": 60 + number,
                "split_sequence": number,
                "case_id": f"test-{number:02d}",
                "source_date": f"2025-03-{number:02d}",
                "source_order": number,
                "original": _entry(str(original.relative_to(tmp_path))),
                "reference": _entry(str(reference.relative_to(tmp_path)), kind=kind),
            }
        )
    path = tmp_path / "split-manifest-19.json"
    path.write_text(json.dumps({"items": items}), encoding="utf-8")
    return path


class _QuietMemory:
    def ingest_user_evidence(self, **_: object) -> None:
        pass

    def selected_evidence(self, **_: object) -> tuple[str, ...]:
        return ()


def _run(tmp_path: Path, **overrides: object) -> dict[str, object]:
    manifest_path = overrides.pop("manifest_path", None)
    arguments: dict[str, object] = {
        "manifest_path": manifest_path or _manifest(tmp_path),
        "namespace": "synthetic-run-namespace",
        "memory_factory": lambda _: _QuietMemory(),
        "generator": lambda **_: "synthetic generated reply",
        "persona_evaluator": lambda **_: {"score": 0.9, "hard_violations": []},
        "reference_evaluator": lambda **_: {"style_score": 0.8},
        "persona_authority": {"authority": "synthetic"},
        "output_path": tmp_path / "local-only-report.json",
        "validation_mode": "synthetic_validation",
    }
    arguments.update(overrides)
    return run_case01(**arguments)  # type: ignore[arg-type]


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
        assert kwargs["original"] == "synthetic case01 original"
        assert kwargs["selected_evidence"] == ("synthetic selected evidence",)
        assert "synthetic held-out reference" not in json.dumps(
            kwargs,
            ensure_ascii=False,
        )
        return {"score": 0.9, "hard_violations": []}

    def reference_evaluator(**kwargs: object) -> dict[str, float]:
        events.append("reference-evaluate")
        assert kwargs["reference_text"] == "synthetic held-out reference"
        return {"style_score": 0.8, "focus_score": 0.7}

    output_path = tmp_path / "local-only-report.json"
    report = run_case01(
        manifest_path=_manifest(tmp_path),
        namespace="synthetic-run-namespace",
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
        "memory-factory:synthetic-run-namespace",
        *(f"ingest:train:train-{number:02d}" for number in range(1, 61)),
        "ingest:test:test-first",
        "select-memory",
        "generate",
        "persona-evaluate",
        "reference-evaluate",
    ]
    assert report == json.loads(output_path.read_text(encoding="utf-8"))
    assert report["case_id"] == "test-first"
    assert report["prefix_case"] == "case01"
    assert report["namespace"] == "synthetic-run-namespace"
    assert report["validation_mode"] == "synthetic_validation"
    assert report["private_world_arm"] == "fixed_disabled"
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "contracts" / "memory_isolation_case01_report.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(Draft202012Validator(schema).iter_errors(report)) == []

    assert "synthetic case01 original" not in json.dumps(report)
    assert "synthetic generated reply" not in json.dumps(report)
    assert "synthetic held-out reference" not in json.dumps(report)


@pytest.mark.parametrize(
    ("validation_mode", "private_world_arm"),
    [
        ("synthetic_validation", "fixed_disabled"),
        ("private_local_validation", "fixed_disabled"),
        ("synthetic_validation", "controlled_projection"),
    ],
)
def test_prefix19_rebuilds_isolated_memory_without_reading_video_references(
    tmp_path: Path,
    validation_mode: str,
    private_world_arm: str,
) -> None:
    events: list[str] = []
    ingested: list[tuple[str, str]] = []
    generator_calls: list[dict[str, object]] = []

    class Memory:
        def ingest_user_evidence(self, *, source_id: str, text: str) -> None:
            ingested.append((source_id, text))

        def selected_evidence(self, *, original: str) -> tuple[str, ...]:
            return (f"evidence for {original[-2:]}",)

    def memory_factory(namespace: str) -> Memory:
        events.append(f"memory:{namespace}")
        return Memory()

    def generator(**kwargs: object) -> str:
        events.append("generator")
        assert set(kwargs) == {"persona_authority", "selected_evidence", "original"}
        generator_calls.append(kwargs)
        return f"synthetic reply {int(str(kwargs['original']).rsplit(' ', 1)[1]):02d}"

    def persona_evaluator(**kwargs: object) -> dict[str, object]:
        events.append(f"persona:{kwargs['reply']}")
        assert set(kwargs) == {"reply", "original", "selected_evidence"}
        assert "synthetic reference" not in json.dumps(kwargs, ensure_ascii=False)
        return {"score": 0.9, "hard_violations": [], "continuity": 0.8}

    def reference_evaluator(**kwargs: object) -> dict[str, object]:
        events.append(f"reference:{kwargs['reply']}")
        assert "synthetic reference" in str(kwargs["reference_text"])
        return {"style_score": 0.8}

    output_path = tmp_path / "local-only-prefix19-report.json"
    report = run_prefix19(
        manifest_path=_manifest_19(tmp_path),
        namespace="synthetic-run",
        memory_factory=memory_factory,
        generator=generator,
        persona_evaluator=persona_evaluator,
        reference_evaluator=reference_evaluator,
        persona_authority={"authority": "synthetic"},
        output_path=output_path,
        validation_mode=validation_mode,
        private_world_arm=private_world_arm,
    )

    cases = report["cases"]
    assert isinstance(cases, list) and len(cases) == 19
    assert report["private_world_arm"] == private_world_arm
    assert all(case["private_world_arm"] == private_world_arm for case in cases)
    assert [case["namespace"] for case in cases] == [
        f"synthetic-run:case{number:02d}" for number in range(1, 20)
    ]
    assert len({case["namespace"] for case in cases}) == 19
    assert len([source_id for source_id, _ in ingested if source_id.startswith("train:")]) == 19 * 60
    assert len([source_id for source_id, _ in ingested if source_id.startswith("test:")]) == sum(range(1, 20))
    assert all("synthetic reply" not in text for _, text in ingested)
    assert len(generator_calls) == 19
    assert [case["test_original_count"] for case in cases] == list(range(1, 20))
    assert [case["reference_status"] for case in cases if case["prefix_case"] in {"case07", "case19"}] == [
        "not_evaluated_media",
        "not_evaluated_media",
    ]
    assert len([event for event in events if event.startswith("persona:")]) == 19
    assert len([event for event in events if event.startswith("reference:")]) == 17
    for number in range(1, 20):
        reply = f"synthetic reply {number:02d}"
        persona_index = events.index(f"persona:{reply}")
        if number not in {7, 19}:
            assert events[persona_index + 1] == f"reference:{reply}"
    assert report == json.loads(output_path.read_text(encoding="utf-8"))
    serialized = json.dumps(report, ensure_ascii=False)
    assert "synthetic test original" not in serialized
    assert "synthetic reply" not in serialized
    assert "references/" not in serialized
    assert str(tmp_path) not in serialized
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "contracts" / "memory_isolation_case01_report.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(Draft202012Validator(schema).iter_errors(report)) == []

    unavailable_child = json.loads(json.dumps(report))
    unavailable_child["cases"][0] = {
        key: value
        for key, value in unavailable_child["cases"][0].items()
        if key
        in {
            "case_id",
            "prefix_case",
            "validation_mode",
            "namespace",
            "train_original_count",
            "test_original_count",
            "private_world_arm",
        }
    }
    unavailable_child["cases"][0].update(status="unavailable", error_code=None)
    missing_text_evaluation = json.loads(json.dumps(report))
    missing_text_evaluation["cases"][0].pop("reference_evaluation")
    media_with_text_evaluation = json.loads(json.dumps(report))
    media_case = next(
        case
        for case in media_with_text_evaluation["cases"]
        if case["reference_status"] == "not_evaluated_media"
    )
    media_case["reference_evaluation"] = {"style_score": 0.8}
    repeated_case = json.loads(json.dumps(report))
    repeated_case["cases"][1]["prefix_case"] = "case01"
    repeated_case["cases"][1]["test_original_count"] = 1
    mismatched_mode = json.loads(json.dumps(report))
    mismatched_mode["cases"][0]["validation_mode"] = (
        "private_local_validation"
        if validation_mode == "synthetic_validation"
        else "synthetic_validation"
    )

    assert list(Draft202012Validator(schema).iter_errors(unavailable_child))
    assert list(Draft202012Validator(schema).iter_errors(missing_text_evaluation))
    assert list(Draft202012Validator(schema).iter_errors(media_with_text_evaluation))
    assert list(Draft202012Validator(schema).iter_errors(repeated_case))
    assert list(Draft202012Validator(schema).iter_errors(mismatched_mode))


def test_prefix19_writes_a_schema_valid_unavailable_case(tmp_path: Path) -> None:
    class Memory:
        def ingest_user_evidence(self, **_: object) -> None:
            pass

        def selected_evidence(self, **_: object) -> tuple[str, ...]:
            return ()

    generator_calls = 0

    def generator(**_: object) -> str:
        nonlocal generator_calls
        generator_calls += 1
        if generator_calls == 2:
            raise RuntimeError("synthetic generator failure")
        return "synthetic reply"

    output_path = tmp_path / "unavailable-prefix19-report.json"
    with pytest.raises(RuntimeError, match="synthetic generator failure"):
        run_prefix19(
            manifest_path=_manifest_19(tmp_path),
            namespace="synthetic-run",
            memory_factory=lambda _: Memory(),
            generator=generator,
            persona_evaluator=lambda **_: {"score": 0.9, "hard_violations": []},
            reference_evaluator=lambda **_: {"style_score": 0.8},
            persona_authority={"authority": "synthetic"},
            output_path=output_path,
            validation_mode="synthetic_validation",
        )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["status"] == "unavailable"
    assert "completed_case_count" not in report
    assert "cases" not in report
    assert report["failed_case"]["prefix_case"] == "case02"
    assert report["failed_case"]["error_code"] == "PREFIX19_GENERATOR_UNAVAILABLE"
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "contracts" / "memory_isolation_case01_report.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(Draft202012Validator(schema).iter_errors(report)) == []
    mismatched_mode = json.loads(json.dumps(report))
    mismatched_mode["failed_case"]["validation_mode"] = "private_local_validation"
    assert list(Draft202012Validator(schema).iter_errors(mismatched_mode))


def test_prefix19_preserves_controlled_arm_on_failure(tmp_path: Path) -> None:
    class Memory:
        def ingest_user_evidence(self, **_: object) -> None:
            pass

        def selected_evidence(self, **_: object) -> tuple[str, ...]:
            return ()

    def generator(**_: object) -> str:
        raise RuntimeError("synthetic controlled failure")

    output_path = tmp_path / "controlled-unavailable-prefix19-report.json"
    with pytest.raises(RuntimeError, match="synthetic controlled failure"):
        run_prefix19(
            manifest_path=_manifest_19(tmp_path),
            namespace="synthetic-controlled-run",
            memory_factory=lambda _: Memory(),
            generator=generator,
            persona_evaluator=lambda **_: {"score": 0.9, "hard_violations": []},
            reference_evaluator=lambda **_: {"style_score": 0.8},
            persona_authority={"authority": "synthetic"},
            output_path=output_path,
            validation_mode="synthetic_validation",
            private_world_arm="controlled_projection",
        )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["private_world_arm"] == "controlled_projection"
    assert report["failed_case"]["private_world_arm"] == "controlled_projection"
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "contracts"
            / "memory_isolation_case01_report.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert list(Draft202012Validator(schema).iter_errors(report)) == []


def test_prefix19_rejects_reference_kind_drift_before_callbacks(tmp_path: Path) -> None:
    manifest_path = _manifest_19(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    test_item = next(item for item in manifest["items"] if item["split"] == "test")
    test_item["reference"]["kind"] = "txt"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    calls: list[str] = []

    def unexpected(*_: object, **__: object) -> object:
        calls.append("called")
        raise AssertionError("manifest validation must run before callbacks")

    with pytest.raises(ValueError, match="^PREFIX19_REFERENCE_KINDS_INVALID$"):
        run_prefix19(
            manifest_path=manifest_path,
            namespace="synthetic-run",
            memory_factory=unexpected,
            generator=unexpected,
            persona_evaluator=unexpected,
            reference_evaluator=unexpected,
            persona_authority={"authority": "synthetic"},
            output_path=tmp_path / "unused.json",
            validation_mode="synthetic_validation",
        )

    assert calls == []


def test_case01_rejects_a_manifest_without_exactly_sixty_train_items(tmp_path: Path) -> None:
    calls: list[str] = []

    def unexpected_callback(*_: object, **__: object) -> object:
        calls.append("called")
        raise AssertionError("train-count validation must run before callbacks")

    with pytest.raises(ValueError, match="^CASE01_TRAIN_COUNT_INVALID$"):
        _run(
            tmp_path,
            manifest_path=_manifest(tmp_path, train_count=59),
            memory_factory=unexpected_callback,
        )

    assert calls == []


def test_case01_rejects_a_non_synthetic_validation_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="^CASE01_VALIDATION_MODE_INVALID$"):
        _run(tmp_path, manifest_path=tmp_path / "not-read.json", validation_mode="real_validation")


def test_case01_report_schema_rejects_non_synthetic_mode_in_both_branches(
    tmp_path: Path,
) -> None:
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "contracts"
            / "memory_isolation_case01_report.schema.json"
        ).read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    completed = _run(tmp_path)
    completed["validation_mode"] = "real_validation"
    unavailable = {
        key: value
        for key, value in completed.items()
        if key
        in {
            "case_id",
            "prefix_case",
            "validation_mode",
            "namespace",
            "train_original_count",
            "test_original_count",
            "private_world_arm",
        }
    }
    unavailable.update(
        status="unavailable",
        error_code="CASE01_GENERATOR_UNAVAILABLE",
    )

    assert list(validator.iter_errors(completed))
    assert list(validator.iter_errors(unavailable))


@pytest.mark.parametrize(
    ("evaluator_name", "result", "error_code"),
    [
        (
            "persona_evaluator",
            {"score": float("nan"), "hard_violations": []},
            "CASE01_PERSONA_EVALUATION_UNAVAILABLE",
        ),
        (
            "reference_evaluator",
            {"style_score": float("inf")},
            "CASE01_REFERENCE_EVALUATION_UNAVAILABLE",
        ),
        (
            "persona_evaluator",
            {"score": 1.5, "hard_violations": []},
            "CASE01_PERSONA_EVALUATION_UNAVAILABLE",
        ),
        (
            "reference_evaluator",
            {"style_score": -0.1},
            "CASE01_REFERENCE_EVALUATION_UNAVAILABLE",
        ),
    ],
)
def test_case01_rejects_non_finite_evaluator_results(
    tmp_path: Path,
    evaluator_name: str,
    result: dict[str, object],
    error_code: str,
) -> None:
    output_path = tmp_path / "local-only-report.json"

    with pytest.raises(ValueError, match="_EVALUATION_INVALID$"):
        _run(tmp_path, **{evaluator_name: lambda **_: result})

    report_text = output_path.read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert report["status"] == "unavailable"
    assert report["error_code"] == error_code
    assert "NaN" not in report_text
    assert "Infinity" not in report_text


@pytest.mark.parametrize(
    ("evaluator_name", "result"),
    [
        (
            "persona_evaluator",
            {"score": 0.9, "hard_violations": [], "D:/private/letter.txt": 1},
        ),
        (
            "reference_evaluator",
            {"style_score": 0.8, "synthetic private reference": 1},
        ),
    ],
)
def test_case01_rejects_uncontracted_metric_names(
    tmp_path: Path,
    evaluator_name: str,
    result: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="_EVALUATION_INVALID$"):
        _run(tmp_path, **{evaluator_name: lambda **_: result})


def test_case01_writes_a_redacted_unavailable_report_when_generator_fails(tmp_path: Path) -> None:
    def generator(**_: object) -> str:
        raise RuntimeError("synthetic callback detail must not reach the report")

    output_path = tmp_path / "local-only-report.json"
    with pytest.raises(RuntimeError, match="synthetic callback detail"):
        _run(tmp_path, generator=generator)

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["status"] == "unavailable"
    assert report["error_code"] == "CASE01_GENERATOR_UNAVAILABLE"
    assert report["case_id"] == "test-first"
    assert report["prefix_case"] == "case01"
    assert report["private_world_arm"] == "fixed_disabled"
    serialized = json.dumps(report, ensure_ascii=False)
    assert "synthetic callback detail" not in serialized
    assert "synthetic case01 original" not in serialized
    assert "synthetic held-out reference" not in serialized
    assert str(tmp_path) not in serialized


def test_case01_rejects_malformed_persona_evaluation_without_counting_it_as_zero(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "local-only-report.json"
    with pytest.raises(ValueError, match="^CASE01_PERSONA_EVALUATION_INVALID$"):
        _run(tmp_path, persona_evaluator=lambda **_: {"score": 0.9, "hard_violations": "invalid"})

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["status"] == "unavailable"
    assert report["error_code"] == "CASE01_PERSONA_EVALUATION_UNAVAILABLE"
    assert "hard_violation_count" not in json.dumps(report)
