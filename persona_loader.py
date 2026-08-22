"""Read, validate, and classify Persona 2.0 data without provider calls."""

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
    INCOMPLETE = "PERSONA_INCOMPLETE"


@dataclass(frozen=True)
class PersonaProfile:
    display_name: str
    locale: str
    summary: str
    required_facets: tuple[str, ...]
    required_modes: tuple[str, ...]


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
    facet: str | None = None


@dataclass(frozen=True)
class PersonaSnapshot:
    schema_version: str | None
    persona_id: str | None
    declarations: tuple[PersonaDeclaration, ...]
    status: Literal["READY", "POLICY_ONLY", "DRAFT"]
    source: Literal["persona_v2", "draft"]
    profile: PersonaProfile | None = None


@dataclass(frozen=True)
class PersonaLoadResult:
    snapshot: PersonaSnapshot
    error_code: PersonaLoadErrorCode | None
    readiness_gaps: tuple[str, ...] = ()

    @property
    def fallback(self) -> bool:
        return self.snapshot.status == "DRAFT"

    @property
    def ready(self) -> bool:
        return self.snapshot.status == "READY"

    @property
    def policy_only(self) -> bool:
        return self.snapshot.status == "POLICY_ONLY"


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
            facet=row.get("facet"),
        )
        for row in payload["declarations"]
    )
    profile_payload = payload.get("profile")
    profile = (
        PersonaProfile(
            display_name=profile_payload["display_name"],
            locale=profile_payload["locale"],
            summary=profile_payload["summary"],
            required_facets=tuple(profile_payload["required_facets"]),
            required_modes=tuple(profile_payload["required_modes"]),
        )
        if isinstance(profile_payload, dict)
        else None
    )
    gaps = _readiness_gaps(profile, declarations)
    if gaps:
        return PersonaLoadResult(
            snapshot=PersonaSnapshot(
                schema_version=payload["schema_version"],
                persona_id=payload["persona_id"],
                declarations=declarations,
                status="POLICY_ONLY",
                source="persona_v2",
                profile=profile,
            ),
            error_code=PersonaLoadErrorCode.INCOMPLETE,
            readiness_gaps=gaps,
        )
    return PersonaLoadResult(
        snapshot=PersonaSnapshot(
            schema_version=payload["schema_version"],
            persona_id=payload["persona_id"],
            declarations=declarations,
            status="READY",
            source="persona_v2",
            profile=profile,
        ),
        error_code=None,
    )


def _readiness_gaps(
    profile: PersonaProfile | None,
    declarations: tuple[PersonaDeclaration, ...],
) -> tuple[str, ...]:
    if profile is None:
        return ("profile",)
    facets = {item.facet for item in declarations if item.facet}
    modes = {
        item.mode
        for item in declarations
        if item.tier == "MODE_STYLE" and item.facet == "MODE_STYLE" and item.mode
    }
    gaps = [
        *(f"facet:{facet}" for facet in profile.required_facets if facet not in facets),
        *(f"mode:{mode}" for mode in profile.required_modes if mode not in modes),
    ]
    has_identity_source = any(
        item.facet == "IDENTITY"
        and item.tier in {"PUBLIC_CANON", "COMMUNITY_SOFT_CANON"}
        for item in declarations
    )
    if not has_identity_source:
        gaps.append("identity_source")
    return tuple(sorted(set(gaps)))


def _draft_result(error_code: PersonaLoadErrorCode) -> PersonaLoadResult:
    return PersonaLoadResult(
        snapshot=PersonaSnapshot(
            schema_version=None,
            persona_id=None,
            declarations=(),
            status="DRAFT",
            source="draft",
            profile=None,
        ),
        error_code=error_code,
    )


__all__ = [
    "PersonaDeclaration",
    "PersonaLoadErrorCode",
    "PersonaLoadResult",
    "PersonaProfile",
    "PersonaSnapshot",
    "load_persona",
]
