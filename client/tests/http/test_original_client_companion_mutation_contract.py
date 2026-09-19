from __future__ import annotations

import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator, ValidationError
import pytest

from original_client_companion_mutation_api import (
    CLEAR_MUTATION_ERROR_CODES,
    CLEAR_MUTATION_REACHABLE_ERROR_CODES,
    CLEAR_MUTATION_READ_STATES,
    COMPANION_MUTATION_ROUTES,
    MEMORY_CLEAR_PATH,
)


ROOT = Path(__file__).parents[2]


def _json_value(value: object) -> object:
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


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
    assert contract["routes"] == _json_value(COMPANION_MUTATION_ROUTES)
    assert contract["error_codes"] == CLEAR_MUTATION_ERROR_CODES
    assert set(CLEAR_MUTATION_ERROR_CODES) == CLEAR_MUTATION_REACHABLE_ERROR_CODES
    assert contract["read_states"] == _json_value(CLEAR_MUTATION_READ_STATES)
    table = (ROOT / "docs" / "B02_ERROR_CODES.md").read_text(encoding="utf-8")
    section = table.split("## P03 clear mutation registry", 1)[1]
    documented = {
        code: {
            "http_status": int(http_status),
            "status": status,
            "retryable": retryable == "是",
        }
        for http_status, code, status, retryable in re.findall(
            r"^\| (\d+) \| `([A-Z][A-Z0-9_]+)` \| ([A-Z]+) \| (是|否) \|",
            section,
            flags=re.MULTILINE,
        )
    }
    assert documented == CLEAR_MUTATION_ERROR_CODES


def test_pending_clear_read_state_is_bound_to_the_public_lifecycle_schema() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "original_client_memory_lifecycle.schema.json").read_text(
            encoding="utf-8"
        )
    )
    pending = CLEAR_MUTATION_READ_STATES["pending"]
    payload = {
        "schema_version": "p03.original-companion-read.v1",
        "status": pending["top_level_status"],
        "capabilities": {
            "memory": {
                "state": pending["memory_state"],
                "reason_code": pending["reason_code"],
            },
            "private_world": {"state": "available"},
            "candidates": {"state": "available"},
        },
    }
    Draft202012Validator(schema).validate(payload)


def test_clear_error_schema_rejects_missing_and_fabricated_public_codes() -> None:
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
    assert contract["error_codes"]["MEMORY_MUTATION_RESULT_INVALID"] == {
        "http_status": 503,
        "status": "UNAVAILABLE",
        "retryable": True,
    }
    for mutation in (
        lambda errors: errors.pop("MEMORY_MUTATION_RESULT_INVALID"),
        lambda errors: errors.update(
            {"BOGUS_REVIEW_CODE": {"http_status": 503, "status": "UNAVAILABLE", "retryable": True}}
        ),
        lambda errors: errors["MEMORY_MUTATION_RESULT_INVALID"].update({"retryable": False}),
    ):
        candidate = json.loads(json.dumps(contract))
        mutation(candidate["error_codes"])
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(candidate)
