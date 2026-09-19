from __future__ import annotations

from pathlib import Path

from tools.persona_evaluator import BYPASS_CATEGORIES, evaluate


ROOT = Path(__file__).resolve().parents[2]


def test_all_synthetic_splits_cover_eight_bypass_categories_and_zero_skip() -> None:
    report = evaluate(ROOT)
    assert report["status"] == "PASS"
    assert report["skipped"] == 0
    assert report["model_called"] is False
    assert report["holdout_used_for_selection"] is False
    assert report["evaluated_cases"] == sum(report["cases"].values())
    assert set(report["bypass_categories"]) == set(BYPASS_CATEGORIES)
    assert all(report["bypass_categories"][category] >= 1 for category in BYPASS_CATEGORIES)
    assert report["safe_negative_cases"] >= 3
    assert report["safe_negative_passed"] == report["safe_negative_cases"]
