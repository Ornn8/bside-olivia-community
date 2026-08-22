"""The B10A scope checker must accept only this batch's files."""

from __future__ import annotations

import pytest

from tools.verify_B10A_scope import check_scope
from tools.verify_b05_scope import current_b05_paths


# Experimental advisory: this standalone fixed-baseline audit is retained for
# diagnostics; B08 composed scope is the blocking gate.
@pytest.mark.experimental
def test_b10a_scope_is_clean() -> None:
    # B05 composition is explicit; the historical B10A allow-list itself is
    # not widened to include ASR files.
    report = check_scope(current_b05_paths())
    assert report["status"] == "PASS", report
    assert report["unexpected_paths"] == []
