from __future__ import annotations

import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator

from original_client_companion_mutation_api import (
    CLEAR_MUTATION_ERROR_CODES,
    CLEAR_MUTATION_REACHABLE_ERROR_CODES,
    CLEAR_MUTATION_READ_STATES,
    COMPANION_MUTATION_ROUTES,
    MEMORY_CLEAR_PATH,
)


ROOT = Path(__file__).parents[2]


def test_clear_route_and_second_confirmation_have_a_machine_contract() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "original_client_companion_mutation_contract.schema.json").read_text(
            encoding="utf-8"
        )
    )
    contract = json.loads(
        (ROOT / "contracts" / "original_client_companion_mutation_contract.json").read_text(
            encoding="utf-8"
        )
    )

    Draft202012Validator(schema).validate(contract)
    assert contract["routes"][MEMORY_CLEAR_PATH]["methods"] == list(
        COMPANION_MUTATION_ROUTES[MEMORY_CLEAR_PATH]["methods"]
    )
    assert set(contract["error_codes"]) == set(CLEAR_MUTATION_ERROR_CODES)
    assert set(CLEAR_MUTATION_ERROR_CODES) == CLEAR_MUTATION_REACHABLE_ERROR_CODES
    assert contract["read_states"] == {
        "statuses": list(CLEAR_MUTATION_READ_STATES["statuses"]),
        "pending_reason": CLEAR_MUTATION_READ_STATES["pending_reason"],
    }
    table = (ROOT / "docs" / "B02_ERROR_CODES.md").read_text(encoding="utf-8")
    section = table.split("## P03 clear mutation registry", 1)[1]
    documented = set(re.findall(r"`([A-Z][A-Z0-9_]+)`", section))
    assert documented == set(CLEAR_MUTATION_ERROR_CODES)
