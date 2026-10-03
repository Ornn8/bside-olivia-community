"""Optional, bounded analysis that can only create review candidates."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
import json
import inspect
import math
from pathlib import Path
from typing import Mapping, Protocol
from weakref import WeakValueDictionary

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
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.retryable = retryable
        super().__init__(code)


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
        questions_port=None,
    ) -> None:
        self.gateway = gateway
        self.timeout_seconds = timeout_seconds
        self.minimum_confidence = minimum_confidence
        self.validator = _load_validator()
        self.questions_port = questions_port

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
            from runtime.reply.jev_questions import configured_questions
            questions_port = self.questions_port or configured_questions()
            if questions_port is not None:
                labels = {'none': '无充分证据', 'boundary_respected': '尊重既有边界',
                          'conflict': '实际关系冲突', 'repair': '明确修复既有冲突'}
                questions = {
                    'candidate': {'instructions': messages[0]['content'] +
                        '只看原文支持的双方互动；角色生成的责备不证明用户有伤害行为。用户单方要求不提升亲密权限。',
                        'criteria': labels},
                    'confidence': {'instructions': '对上述候选是否有清晰原文证据？不清楚选 low；此数字仅为离散审核置信档位。',
                                   'criteria': {'low': '不足或不确定', 'medium': '有明确证据', 'high': '双方证据充分明确'}},
                }
                answers = await questions_port.ask(request.to_dict(), questions, purpose='private_world_candidate')
                if (not isinstance(answers, dict) or set(answers) != set(questions)
                        or any(not isinstance(v, str) or v not in questions[k]['criteria'] for k, v in answers.items())):
                    raise ValueError('JEV_RESPONSE_INVALID')
                payload = {'schema_version': 'p03.private-world-candidate.v1',
                           'candidate': answers['candidate'],
                           'confidence': {'low': 0.3, 'medium': 0.7, 'high': 0.9}[answers['confidence']],
                           'summary': '待人工审核：' + labels[answers['candidate']], 'evidence_spans': []}
            else:
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
                payload = json.loads(response.text.strip(), parse_constant=_reject_json_constant)
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
        except (asyncio.TimeoutError, GatewayError) as exc:
            raise PrivateWorldCandidateAnalysisError(
                "PRIVATE_WORLD_CANDIDATE_ANALYSIS_UNAVAILABLE",
                retryable=isinstance(exc, asyncio.TimeoutError) or exc.retryable,
            ) from exc
        except (
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            raise PrivateWorldCandidateAnalysisError(
                "PRIVATE_WORLD_CANDIDATE_ANALYSIS_UNAVAILABLE",
                # The JEV port reports transport failures as ValueError codes.
                # Only these known transient codes may spend another attempt.
                retryable=str(exc) in {
                    'JEV_UNAVAILABLE', 'JEV_TIMEOUT',
                    *(f'JEV_HTTP_{status}' for status in (429, 500, 502, 503, 504, 529)),
                    *(f'JEV_PROVIDER_HTTP_{status}' for status in (429, 500, 502, 503, 504, 529)),
                },
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
    from runtime.reply.jev_questions import JevQuestionsPort
    endpoint = environ.get('OLIVIA_JEV_DECISION_URL', '').strip()
    try:
        questions_port = JevQuestionsPort(endpoint, token=environ.get('COMPANION_CLASSIFIER_TOKEN', '')) if endpoint else None
    except ValueError:
        return PrivateWorldCandidateRuntime(null, None, 'unavailable', 'none',
                                            'PRIVATE_WORLD_CANDIDATE_GATEWAY_UNAVAILABLE', True)
    if not gateway_ready and questions_port is None:
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
            questions_port=questions_port,
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
        "jev" if questions_port is not None else "llm_gateway",
        None,
        True,
    )


class CandidateDeliveryStatus(StrEnum):
    CREATED = "CREATED"
    DUPLICATE = "DUPLICATE"
    SKIPPED = "SKIPPED"
    UNAVAILABLE = "UNAVAILABLE"


_ANALYSIS_ATTEMPTS = 3
_analysis_locks = WeakValueDictionary()


async def _persist_candidate(persist):
    if persist is not None:
        result = persist()
        if inspect.isawaitable(result):
            await result


async def _candidate_proposal(analyzer, request, checkpoint, persist):
    if checkpoint is None:
        return await analyzer.analyze(request)
    identity = f'private-world-candidate:{request.source_letter_id}:{request.source_reply_revision}'
    previous = checkpoint.get('candidate_analysis_operation_id')
    if previous is not None and previous != identity:
        for key in tuple(checkpoint):
            if key.startswith('candidate_analysis_'):
                checkpoint.pop(key)
    checkpoint['candidate_analysis_operation_id'] = identity
    if 'candidate_analysis_result' in checkpoint:
        saved = checkpoint['candidate_analysis_result']
        if saved is None:
            return None
        try:
            confidence = saved['confidence']
            summary = saved['summary']
            if (not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
                    or not math.isfinite(confidence) or not 0 <= confidence <= 1
                    or not isinstance(summary, str) or not 1 <= len(summary) <= _EXCERPT_LIMIT):
                raise ValueError('invalid cached proposal')
            return PrivateWorldCandidateProposal(CandidateType(saved['candidate_type']), confidence, summary)
        except (KeyError, TypeError, ValueError):
            checkpoint.update(candidate_analysis_status='FAILED', candidate_analysis_retry_status='TERMINAL_REJECTION',
                              candidate_analysis_failure_reason='PRIVATE_WORLD_CANDIDATE_CACHE_INVALID')
            checkpoint.pop('candidate_analysis_retry_at', None)
            await _persist_candidate(persist)
            raise PrivateWorldCandidateAnalysisError('PRIVATE_WORLD_CANDIDATE_CACHE_INVALID') from None
    attempts = checkpoint.get('candidate_analysis_attempts', 0)
    if attempts >= _ANALYSIS_ATTEMPTS:
        checkpoint['candidate_analysis_retry_status'] = 'EXHAUSTED'
    if checkpoint.get('candidate_analysis_retry_status') in {'TERMINAL_REJECTION', 'EXHAUSTED'}:
        checkpoint.pop('candidate_analysis_retry_at', None)
        await _persist_candidate(persist)
        raise PrivateWorldCandidateAnalysisError('PRIVATE_WORLD_CANDIDATE_ANALYSIS_UNAVAILABLE')
    now = datetime.now().timestamp()
    if now < checkpoint.get('candidate_analysis_retry_at', 0):
        raise PrivateWorldCandidateAnalysisError('PRIVATE_WORLD_CANDIDATE_ANALYSIS_UNAVAILABLE')
    checkpoint['candidate_analysis_attempts'] = attempts + 1
    try:
        await _persist_candidate(persist)
    except Exception:
        checkpoint['candidate_analysis_attempts'] = attempts
        raise
    try:
        proposal = await analyzer.analyze(request)
        if proposal is not None and not isinstance(proposal, PrivateWorldCandidateProposal):
            raise PrivateWorldCandidateAnalysisError('PRIVATE_WORLD_CANDIDATE_ANALYSIS_UNAVAILABLE')
    except asyncio.CancelledError:
        raise
    except (RuntimeError, OSError, TypeError, ValueError) as exc:
        checkpoint['candidate_analysis_failure_reason'] = 'PRIVATE_WORLD_CANDIDATE_ANALYSIS_UNAVAILABLE'
        if not getattr(exc, 'retryable', not isinstance(exc, (TypeError, ValueError))):
            checkpoint['candidate_analysis_retry_status'] = 'TERMINAL_REJECTION'
            checkpoint.pop('candidate_analysis_retry_at', None)
        elif checkpoint['candidate_analysis_attempts'] >= _ANALYSIS_ATTEMPTS:
            checkpoint['candidate_analysis_retry_status'] = 'EXHAUSTED'
            checkpoint.pop('candidate_analysis_retry_at', None)
        else:
            checkpoint['candidate_analysis_retry_at'] = now + 30 * 2 ** attempts
        await _persist_candidate(persist)
        raise
    checkpoint['candidate_analysis_result'] = (None if proposal is None else {
        'candidate_type': proposal.candidate_type.value, 'confidence': proposal.confidence,
        'summary': proposal.summary,
    })
    checkpoint['candidate_analysis_status'] = 'COMPLETED'
    checkpoint.pop('candidate_analysis_retry_at', None)
    checkpoint.pop('candidate_analysis_retry_status', None)
    checkpoint.pop('candidate_analysis_failure_reason', None)
    # Save the model result before local SQLite delivery. A local write failure
    # must replay this proposal rather than pay to analyze the exchange again.
    await _persist_candidate(persist)
    return proposal


async def deliver_private_world_candidate(
    analyzer: PrivateWorldCandidateAnalyzer,
    store: SQLitePrivateWorldCandidateStore,
    request: PrivateWorldCandidateRequest,
    *,
    checkpoint: dict | None = None,
    persist=None,
) -> CandidateDeliveryStatus:
    """Share a durable extraction across replay; only local delivery retries."""
    if checkpoint is None:
        return await _deliver_candidate(analyzer, store, request, checkpoint, persist)
    lock = _analysis_locks.setdefault(id(checkpoint), asyncio.Lock())
    async with lock:
        return await _deliver_candidate(analyzer, store, request, checkpoint, persist)


async def _deliver_candidate(analyzer, store, request, checkpoint, persist):

    try:
        proposal = await _candidate_proposal(analyzer, request, checkpoint, persist)
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
