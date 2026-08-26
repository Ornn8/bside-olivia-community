from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from original_client_companion_mutation_api import (
    COMPANION_MUTATION_ERROR_CODES,
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
    assert contract["routes"][MEMORY_CLEAR_PATH] == {
        "methods": list(COMPANION_MUTATION_ROUTES[MEMORY_CLEAR_PATH]["methods"]),
        "confirmation": COMPANION_MUTATION_ROUTES[MEMORY_CLEAR_PATH]["confirmation"],
    }
    assert contract["error_codes"]["MEMORY_CLEAR_CONFIRMATION_REQUIRED"] == (
        COMPANION_MUTATION_ERROR_CODES["MEMORY_CLEAR_CONFIRMATION_REQUIRED"]
    )
