import json
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from runtime.reply.reply_context import (
    IntimacyRequest,
    IntimacyTier,
    KnownContinuationFact,
    PrivateBehaviorView,
    ReplyContext,
    ReplyMode,
    RelationshipStage,
    TrustedTime,
    TrustedWorldFact,
)
from runtime.reply.reply_reviewer import (
    JsonReviewerAdapter,
    NullReviewer,
    ReviewReference,
    ReviewResult,
    ReviewerScores,
    ReviewerConfig,
    ReviewStatus,
    ReviewVerdict,
)


ROOT = Path(__file__).resolve().parents[2]


class _Transport:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    def review_json(
        self,
        request: dict[str, object],
        *,
        model: str,
        timeout_seconds: float,
    ) -> object:
        self.requests.append(request)
        return self.response


class _FailingTransport:
    def review_json(self, request, *, model, timeout_seconds):
        raise RuntimeError("private provider failure details")


def _valid_response() -> dict[str, object]:
    return {
        "schema_version": "p02.reply-review.v2",
        "status": "completed",
        "verdict": "pass",
        "violations": [],
        "intimacy_request": "none",
        "intimacy_claims": [],
        "scores": {
            "persona_consistency": 92,
            "factual_consistency": 94,
            "relationship_boundary": 96,
            "mode_compliance": 98,
        },
    }


def test_valid_reviewer_json_becomes_a_typed_result_from_limited_context() -> None:
    transport = _Transport(_valid_response())
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
        world_facts=(
            TrustedWorldFact(
                "fact.synthetic",
                "source.synthetic",
                "Synthetic fact.",
            ),
        ),
        private_behavior=PrivateBehaviorView(
            relationship_stage=RelationshipStage.CLOSE,
            intimacy_ceiling=IntimacyTier.LIGHT_CONTACT,
            granted_intimacy=IntimacyTier.LIGHT_CONTACT,
            known_continuations=(
                KnownContinuationFact(
                    "class.known",
                    "她已经知道下周课程会调整。",
                ),
            )
        ),
    )
    adapter = JsonReviewerAdapter(
        transport,
        ReviewerConfig(model="reviewer-small", timeout_seconds=5),
    )

    result = adapter.review("A synthetic candidate.", context)

    assert result.status is ReviewStatus.COMPLETED
    assert result.verdict is ReviewVerdict.PASS
    assert result.intimacy_request is IntimacyRequest.NONE
    assert result.intimacy_claims == ()
    assert result.scores.mode_compliance == 98
    request = transport.requests[0]
    assert request["world_facts"][0]["fact_id"] == "fact.synthetic"
    assert request["known_continuations"] == [
        {
            "fact_id": "class.known",
            "statement": "她已经知道下周课程会调整。",
        }
    ]
    assert "private_behavior" not in request
    assert request["relationship_context"] == {
        "relationship_stage": "close",
        "intimacy_ceiling": "light_contact",
        "granted_intimacy": "light_contact",
    }
    assert "trust" not in repr(request)


def test_character_refusal_sample_produces_no_violation() -> None:
    adapter = JsonReviewerAdapter(_Transport(_valid_response()), ReviewerConfig("reviewer-small"))
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
    )
    result = adapter.review(
        "今天不想见面，我想自己休息。",
        context,
    )
    assert (result.status, result.verdict, result.violations) == (ReviewStatus.COMPLETED, ReviewVerdict.PASS, ())


def test_public_schema_rejects_unknown_fields_and_out_of_range_scores() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "reply_review.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    valid = {
        "schema_version": "p02.reply-review.v2",
        "status": "completed",
        "verdict": "rewrite",
        "violations": [
            {
                "code": "UNAUTHORIZED_SHARED_HISTORY",
                "severity": "hard",
                "evidence": {"start": 2, "end": 8},
            }
        ],
        "intimacy_request": "requested",
        "intimacy_claims": [
            {
                "claim_id": "intimacy.synthetic",
                "tier": "light_contact",
                "start": 2,
                "end": 8,
            }
        ],
        "scores": {
            "persona_consistency": 80,
            "factual_consistency": 75,
            "relationship_boundary": 60,
            "mode_compliance": 100,
        },
    }

    assert list(validator.iter_errors(valid)) == []
    invalid = {**valid, "hidden_state": {"trust": 0.9}}
    assert list(validator.iter_errors(invalid))
    invalid_score = {
        **valid,
        "scores": {**valid["scores"], "mode_compliance": 101},
    }
    assert list(validator.iter_errors(invalid_score))


def test_invalid_provider_json_returns_a_sanitized_typed_failure() -> None:
    invalid = _valid_response()
    invalid["scores"] = {
        **invalid["scores"],
        "mode_compliance": 101,
    }
    transport = _Transport(invalid)
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
    )

    result = JsonReviewerAdapter(
        transport,
        ReviewerConfig("reviewer-small"),
    ).review("Synthetic candidate.", context)

    assert result.status is ReviewStatus.INVALID_RESPONSE
    assert result.verdict is ReviewVerdict.UNAVAILABLE
    assert result.error_code == "REVIEWER_RESPONSE_INVALID"
    assert result.violations == ()
    assert result.intimacy_claims is None


def test_disabled_and_failed_reviewers_return_sanitized_unavailable_results() -> None:
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
    )
    transport = _Transport({})

    disabled = JsonReviewerAdapter(
        transport,
        ReviewerConfig("reviewer-small", enabled=False),
    ).review("Synthetic candidate.", context)
    null_result = NullReviewer().review("Synthetic candidate.", context)
    failed = JsonReviewerAdapter(
        _FailingTransport(),
        ReviewerConfig("reviewer-small"),
    ).review("Synthetic candidate.", context)

    assert disabled.status is ReviewStatus.DISABLED
    assert null_result.status is ReviewStatus.DISABLED
    assert transport.requests == []
    assert failed.status is ReviewStatus.UNAVAILABLE
    assert failed.error_code == "REVIEWER_UNAVAILABLE"
    assert "private" not in str(failed)


def test_reviewer_request_accepts_only_short_identified_reference_summaries() -> None:
    transport = _Transport(_valid_response())
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
    )

    JsonReviewerAdapter(
        transport,
        ReviewerConfig("reviewer-small"),
    ).review(
        "Synthetic candidate.",
        context,
        references=(
            ReviewReference(
                "ref.synthetic",
                "Short synthetic summary.",
            ),
        ),
    )

    assert transport.requests[0]["references"] == [
        {
            "reference_id": "ref.synthetic",
            "summary": "Short synthetic summary.",
        }
    ]
    assert set(transport.requests[0]) == {
        "candidate",
        "mode",
        "output_constraints",
        "world_facts",
        "known_continuations",
        "relationship_context",
        "references",
    }


def test_reviewer_parses_candidate_bound_intimacy_evidence() -> None:
    response = _valid_response()
    response["intimacy_request"] = "requested"
    response["intimacy_claims"] = [
        {
            "claim_id": "intimacy.synthetic",
            "tier": "light_contact",
            "start": 0,
            "end": 9,
        }
    ]
    result = JsonReviewerAdapter(
        _Transport(response),
        ReviewerConfig("reviewer-small"),
    ).review("Synthetic candidate.", ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
    ))

    assert result.intimacy_request is IntimacyRequest.REQUESTED
    assert len(result.intimacy_claims) == 1
    assert result.intimacy_claims[0].claim_id == "intimacy.synthetic"
    assert result.intimacy_claims[0].tier is IntimacyTier.LIGHT_CONTACT


def test_reviewer_rejects_missing_duplicate_or_out_of_range_intimacy_claims() -> None:
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
    )
    missing = _valid_response()
    missing.pop("intimacy_claims")
    missing_request = _valid_response()
    missing_request.pop("intimacy_request")
    duplicate = _valid_response()
    duplicate["intimacy_claims"] = [
        {"claim_id": "same", "tier": "none", "start": 0, "end": 1},
        {"claim_id": "same", "tier": "none", "start": 1, "end": 2},
    ]
    outside = _valid_response()
    outside["intimacy_claims"] = [
        {"claim_id": "outside", "tier": "light_contact", "start": 0, "end": 99}
    ]
    invalid_tier = _valid_response()
    invalid_tier["intimacy_claims"] = [
        {"claim_id": "invalid", "tier": "sexual", "start": 0, "end": 1}
    ]

    for response in (
        missing,
        missing_request,
        duplicate,
        outside,
        invalid_tier,
    ):
        result = JsonReviewerAdapter(
            _Transport(response),
            ReviewerConfig("reviewer-small"),
        ).review("Synthetic candidate.", context)
        assert result.status is ReviewStatus.INVALID_RESPONSE
        assert result.verdict is ReviewVerdict.UNAVAILABLE
        assert result.intimacy_claims is None


def test_review_result_distinguishes_completed_empty_from_missing_assessment() -> None:
    with pytest.raises(ValueError):
        ReviewResult(
            ReviewStatus.COMPLETED,
            ReviewVerdict.PASS,
            (),
            ReviewerScores(100, 100, 100, 100),
            None,
            None,
        )
    with pytest.raises(ValueError):
        ReviewResult(
            ReviewStatus.UNAVAILABLE,
            ReviewVerdict.UNAVAILABLE,
            (),
            ReviewerScores(0, 0, 0, 0),
            IntimacyRequest.NONE,
            (),
            error_code="REVIEWER_UNAVAILABLE",
        )
