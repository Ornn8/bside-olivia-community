from __future__ import annotations

import pytest

from letter_status import (
    LetterStatusError,
    OriginalLetterStatus,
    original_letter_status,
    original_letter_status_or_failed,
)


@pytest.mark.parametrize(
    ("internal", "wire"),
    [
        ("PENDING", 1),
        ("processing", 1),
        (" COMPLETED ", 4),
        ("REPLIED", 4),
        ("FAILED", 5),
        ("CANCELED", 5),
        ("cancelled", 5),
    ],
)
def test_internal_states_map_to_original_numeric_enum(internal: str, wire: int) -> None:
    assert original_letter_status(internal) == wire


def test_existing_valid_wire_values_are_preserved() -> None:
    assert original_letter_status(OriginalLetterStatus.PENDING) == 1
    assert original_letter_status(4) == 4
    assert original_letter_status(5) == 5


@pytest.mark.parametrize("value", [None, True, False, 0, 2, 3, 6, "", "READY", [], {}])
def test_unknown_states_are_rejected(value: object) -> None:
    with pytest.raises(LetterStatusError):
        original_letter_status(value)


def test_public_fallback_fails_closed_as_failed() -> None:
    assert original_letter_status_or_failed("not-a-state") == 5
    assert original_letter_status_or_failed(None) == 5
