"""Read and validate Persona 2.0 data without activating it."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from jsonschema import Draft202012Validator, SchemaError, ValidationError


DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent / "contracts" / "persona_v2.schema.json"


class PersonaLoadErrorCode(StrEnum):
    FILE_MISSING = "PERSONA_FILE_MISSING"
    READ_FAILED = "PERSONA_READ_FAILED"
    JSON_INVALID = "PERSONA_JSON_INVALID"
    SCHEMA_INVALID = "PERSONA_SCHEMA_INVALID"
    SCHEMA_UNAVAILABLE = "PERSONA_SCHEMA_UNAVAILABLE"
    RIGHTS_BLOCKED = "PERSONA_RIGHTS_BLOCKED"


@dataclass(frozen=True)
class PersonaDeclaration:
    declaration_id: str
    source_id: str
    tier: str
    confidence: str
    rights_status: str
    allowed_public_release: bool
    statement: str
    mode: str | None


@dataclass(frozen=True)
class PersonaSnapshot:
    schema_version: str | None
    persona_id: str | None
    declarations: tuple[PersonaDeclaration, ...]
    status: Literal["READY", "DRAFT"]
    source: Literal["persona_v2", "draft"]


@dataclass(frozen=True)
class PersonaLoadResult:
    snapshot: PersonaSnapshot
    error_code: PersonaLoadErrorCode | None

    @property
    def fallback(self) -> bool:
        return self.snapshot.status == "DRAFT"


def load_persona(
    path: str | Path,
    *,
    schema_path: str | Path = DEFAULT_SCHEMA_PATH,
) -> PersonaLoadResult:
    persona_path = Path(path)
    if not persona_path.is_file():
        return _draft_result(PersonaLoadErrorCode.FILE_MISSING)
    try:
        payload = json.loads(persona_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _draft_result(PersonaLoadErrorCode.JSON_INVALID)
    except (OSError, UnicodeError):
        return _draft_result(PersonaLoadErrorCode.READ_FAILED)
    try:
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError):
        return _draft_result(PersonaLoadErrorCode.SCHEMA_UNAVAILABLE)
    try:
        Draft202012Validator(schema).validate(payload)
    except ValidationError:
        return _draft_result(PersonaLoadErrorCode.SCHEMA_INVALID)
    if any(not row["allowed_public_release"] for row in payload["declarations"]):
        return _draft_result(PersonaLoadErrorCode.RIGHTS_BLOCKED)
    declarations = tuple(
        PersonaDeclaration(
            declaration_id=row["declaration_id"],
            source_id=row["source_id"],
            tier=row["tier"],
            confidence=row["confidence"],
            rights_status=row["rights_status"],
            allowed_public_release=row["allowed_public_release"],
            statement=row["statement"],
            mode=row.get("mode"),
        )
        for row in payload["declarations"]
    )
    return PersonaLoadResult(
        snapshot=PersonaSnapshot(
            schema_version=payload["schema_version"],
            persona_id=payload["persona_id"],
            declarations=declarations,
            status="READY",
            source="persona_v2",
        ),
        error_code=None,
    )


def _draft_result(error_code: PersonaLoadErrorCode) -> PersonaLoadResult:
    return PersonaLoadResult(
        snapshot=PersonaSnapshot(
            schema_version=None,
            persona_id=None,
            declarations=(),
            status="DRAFT",
            source="draft",
        ),
        error_code=error_code,
    )


__all__ = [
    "PersonaDeclaration",
    "PersonaLoadErrorCode",
    "PersonaLoadResult",
    "PersonaSnapshot",
    "load_persona",
]
