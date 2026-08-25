"""Optional, bounded analysis that can only create review candidates."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
import json
from pathlib import Path
from typing import Mapping, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from llm_gateway import Gateway, GatewayError
from private_world_candidates import (
    CandidateStatus,
    CandidateType,
    CandidateWriteStatus,
    PrivateWorldCandidate,
    PrivateWorldCandidateError,
    SQLitePrivateWorldCandidateStore,
    candidate_identity,
)
from private_world_port import PrivateWorldCharacterView


_EXCERPT_LIMIT = 1200
_CANDIDATE_LIFETIME = timedelta(days=30)
_SCHEMA_PATH = (
    Path(__file__).resolve().parent
    / "contracts"
    / "private_world_candidate_analysis.schema.json"
)
def _excerpt(value: str) -> str:
    normalized = " ".join(value.split())
    return normalized[:_EXCERPT_LIMIT]


@dataclass(frozen=True)
class PrivateWorldCandidateRequest:
    source_letter_id: str
    source_reply_revision: int
    user_excerpt: str
    canonical_excerpt: str
    character_view: PrivateWorldCharacterView
    occurred_at: datetime

    @classmethod
    def create(
        cls,
        *,
        source_letter_id: str,
        source_reply_revision: int,
        user_message: str,
        canonical_reply: str,
        character_view: PrivateWorldCharacterView,
        occurred_at: datetime,
    ) -> "PrivateWorldCandidateRequest":
        return cls(
            source_letter_id=source_letter_id,
            source_reply_revision=source_reply_revision,
            user_excerpt=_excerpt(user_message),
            canonical_excerpt=_excerpt(canonical_reply),
            character_view=character_view,
            occurred_at=occurred_at,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "user_excerpt": self.user_excerpt,
            "canonical_excerpt": self.canonical_excerpt,
            "character_view": self.character_view.to_dict(),
        }


@dataclass(frozen=True)
class PrivateWorldCandidateProposal:
    candidate_type: CandidateType
    confidence: float
    summary: str


class PrivateWorldCandidateAnalyzer(Protocol):
    async def analyze(
        self,
        request: PrivateWorldCandidateRequest,
    ) -> PrivateWorldCandidateProposal | None: ...


class NullPrivateWorldCandidateAnalyzer:
    async def analyze(
        self,
        request: PrivateWorldCandidateRequest,
    ) -> None:
        return None


class PrivateWorldCandidateAnalysisError(RuntimeError):
    pass


def _load_validator() -> Draft202012Validator:
    try:
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        SchemaError,
        TypeError,
        ValueError,
    ) as exc:
        raise PrivateWorldCandidateAnalysisError(
            "PRIVATE_WORLD_CANDIDATE_SCHEMA_UNAVAILABLE"
        ) from exc


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


class GatewayPrivateWorldCandidateAnalyzer:
    """Classify a bounded canonical exchange into a review-only proposal."""

    def __init__(
        self,
        gateway: Gateway,
        *,
        timeout_seconds: float,
        minimum_confidence: float = 0.7,
    ) -> None:
        self.gateway = gateway
        self.timeout_seconds = timeout_seconds
        self.minimum_confidence = minimum_confidence
        self.validator = _load_validator()

    async def analyze(
        self,
        request: PrivateWorldCandidateRequest,
    ) -> PrivateWorldCandidateProposal | None:
        messages = (
            {
                "role": "system",
                "content": (
                    "Return exactly one JSON object using schema_version "
                    "p03.private-world-candidate.v1. Candidate may only be none, "
                    "boundary_respected, conflict, or repair. This is a review "
                    "suggestion, never a command. Do not infer or change relationship "
                    "stage, nicknames, home access, continuation facts, or hidden scores. "
                    "Use a short summary and an empty evidence_spans array."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    request.to_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
            },
        )
        try:
            response = await asyncio.wait_for(
                self.gateway.complete(
                    messages,
                    request_id=(
                        "private-world-candidate:"
                        f"{request.source_letter_id}:{request.source_reply_revision}"
                    ),
                ),
                timeout=self.timeout_seconds,
            )
            payload = json.loads(
                response.text.strip(),
                parse_constant=_reject_json_constant,
            )
            if list(self.validator.iter_errors(payload)):
                raise ValueError("candidate response does not match schema")
            candidate = payload["candidate"]
            confidence = float(payload["confidence"])
            if candidate == "none" or confidence < self.minimum_confidence:
                return None
            return PrivateWorldCandidateProposal(
                candidate_type=CandidateType(candidate),
                confidence=confidence,
                summary=str(payload["summary"]),
            )
        except (
            asyncio.TimeoutError,
            GatewayError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            raise PrivateWorldCandidateAnalysisError(
                "PRIVATE_WORLD_CANDIDATE_ANALYSIS_UNAVAILABLE"
            ) from exc


@dataclass(frozen=True)
class PrivateWorldCandidateRuntime:
    analyzer: PrivateWorldCandidateAnalyzer
    store: SQLitePrivateWorldCandidateStore | None
    status: str
    provider: str
    reason_code: str | None
    enabled: bool

    def public_status(self) -> dict[str, object]:
        return {
            "status": self.status,
            "provider": self.provider if self.status == "available" else "none",
            "reason_code": self.reason_code,
            "enabled": self.enabled,
            "network_called": False,
        }


def _enabled(value: object) -> bool | None:
    if value is None:
        return False
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def create_private_world_candidate_runtime(
    gateway: Gateway,
    *,
    database_path: Path | None,
    gateway_ready: bool,
    environ: Mapping[str, str],
) -> PrivateWorldCandidateRuntime:
    enabled = _enabled(environ.get("OLIVIA_PRIVATE_WORLD_CANDIDATES_ENABLED"))
    null = NullPrivateWorldCandidateAnalyzer()
    if enabled is None:
        return PrivateWorldCandidateRuntime(
            null,
            None,
            "unavailable",
            "none",
            "PRIVATE_WORLD_CANDIDATES_ENABLED_INVALID",
            False,
        )
    if not enabled:
        return PrivateWorldCandidateRuntime(
            null,
            None,
            "disabled",
            "none",
            "PRIVATE_WORLD_CANDIDATES_DISABLED",
            False,
        )
    if not gateway_ready:
        return PrivateWorldCandidateRuntime(
            null,
            None,
            "unavailable",
            "none",
            "PRIVATE_WORLD_CANDIDATE_GATEWAY_UNAVAILABLE",
            True,
        )
    if database_path is None:
        return PrivateWorldCandidateRuntime(
            null,
            None,
            "unavailable",
            "none",
            "PRIVATE_WORLD_CANDIDATE_STORAGE_UNAVAILABLE",
            True,
        )
    try:
        analyzer = GatewayPrivateWorldCandidateAnalyzer(
            gateway,
            timeout_seconds=10.0,
        )
    except PrivateWorldCandidateAnalysisError:
        return PrivateWorldCandidateRuntime(
            null,
            None,
            "unavailable",
            "none",
            "PRIVATE_WORLD_CANDIDATE_SCHEMA_UNAVAILABLE",
            True,
        )
    try:
        store = SQLitePrivateWorldCandidateStore(database_path)
    except (OSError, PrivateWorldCandidateError, RuntimeError, ValueError):
        return PrivateWorldCandidateRuntime(
            null,
            None,
            "unavailable",
            "none",
            "PRIVATE_WORLD_CANDIDATE_STORAGE_UNAVAILABLE",
            True,
        )
    return PrivateWorldCandidateRuntime(
        analyzer,
        store,
        "available",
        "llm_gateway",
        None,
        True,
    )


class CandidateDeliveryStatus(StrEnum):
    CREATED = "CREATED"
    DUPLICATE = "DUPLICATE"
    SKIPPED = "SKIPPED"
    UNAVAILABLE = "UNAVAILABLE"


async def deliver_private_world_candidate(
    analyzer: PrivateWorldCandidateAnalyzer,
    store: SQLitePrivateWorldCandidateStore,
    request: PrivateWorldCandidateRequest,
) -> CandidateDeliveryStatus:
    """Analyze once and persist only a bounded pending proposal."""

    try:
        proposal = await analyzer.analyze(request)
        if proposal is None:
            return CandidateDeliveryStatus.SKIPPED
        if not isinstance(proposal, PrivateWorldCandidateProposal):
            return CandidateDeliveryStatus.UNAVAILABLE
        candidate = PrivateWorldCandidate(
            candidate_id=candidate_identity(
                request.source_letter_id,
                request.source_reply_revision,
                proposal.candidate_type,
            ),
            source_letter_id=request.source_letter_id,
            source_reply_revision=request.source_reply_revision,
            candidate_type=proposal.candidate_type,
            summary=proposal.summary,
            confidence=proposal.confidence,
            status=CandidateStatus.PENDING,
            created_at=request.occurred_at,
            expires_at=request.occurred_at + _CANDIDATE_LIFETIME,
        )
        written = await asyncio.to_thread(store.add, candidate)
    except (PrivateWorldCandidateError, OSError, RuntimeError, TypeError, ValueError):
        return CandidateDeliveryStatus.UNAVAILABLE
    return (
        CandidateDeliveryStatus.CREATED
        if written is CandidateWriteStatus.CREATED
        else CandidateDeliveryStatus.DUPLICATE
    )


__all__ = [
    "CandidateDeliveryStatus",
    "GatewayPrivateWorldCandidateAnalyzer",
    "NullPrivateWorldCandidateAnalyzer",
    "PrivateWorldCandidateAnalyzer",
    "PrivateWorldCandidateAnalysisError",
    "PrivateWorldCandidateProposal",
    "PrivateWorldCandidateRequest",
    "PrivateWorldCandidateRuntime",
    "create_private_world_candidate_runtime",
    "deliver_private_world_candidate",
]
