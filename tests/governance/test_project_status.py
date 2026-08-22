from __future__ import annotations

from pathlib import Path

from tools.verify_project_status import ALLOWED_STATUSES, _status_rows, check_documents


ROOT = Path(__file__).resolve().parents[2]


def test_canonical_governance_documents_pass_static_checks() -> None:
    assert check_documents(ROOT) == []


def test_status_only_exposes_state_semantics_not_work_item_rows() -> None:
    status_text = (ROOT / "docs" / "STATUS.md").read_text(encoding="utf-8")
    rows = _status_rows(status_text)
    assert not rows
    assert all(f"`{status}`" in status_text for status in ALLOWED_STATUSES)


def test_static_check_rejects_broken_link_absolute_path_secret_and_status(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "PROJECT_MANAGEMENT.md").write_text(
        "# Project management\n[broken](missing.md) C:\\private\\file\n",
        encoding="utf-8",
    )
    (docs / "STATUS.md").write_text(
        "当前状态的唯一 source of truth 是本文件。 api_key=1234567890123\n"
        "| Work item | Status | Fact |\n| --- | --- | --- |\n| X | PASS | bad |\n",
        encoding="utf-8",
    )

    findings = check_documents(tmp_path)

    assert any("broken relative link" in finding for finding in findings)
    assert any("absolute local path" in finding for finding in findings)
    assert any("secret-like text" in finding for finding in findings)
    assert any("invalid status" in finding for finding in findings)
