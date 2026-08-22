from __future__ import annotations

from tools.verify_B07_scope import check_scope


def test_b07_scope_is_clean() -> None:
    report = check_scope()
    assert report["status"] == "PASS", report
    assert report["unexpected_paths"] == []
    assert report["media_paths"] == []


def test_b07_scope_validates_committed_tranche_on_clean_checkout() -> None:
    report = check_scope()
    assert "tools/visual_driver.py" in report["changed_paths"], report
