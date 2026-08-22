import json
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from reply_context import ReplyContext, ReplyMode, TrustedTime, TrustedWorldFact
from reply_reviewer import (
    JsonReviewerAdapter,
    NullReviewer,
    ReviewReference,
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


def test_valid_reviewer_json_becomes_a_typed_result_from_limited_context() -> None:
    transport = _Transport(
        {
            "schema_version": "p02.reply-review.v1",
            "status": "completed",
            "verdict": "pass",
            "violations": [],
            "scores": {
                "persona_consistency": 92,
                "factual_consistency": 94,
                "relationship_boundary": 96,
                "mode_compliance": 98,
            },
        }
    )
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
        world_facts=(
            TrustedWorldFact("fact.synthetic", "source.synthetic", "Synthetic fact."),
        ),
    )
    adapter = JsonReviewerAdapter(
        transport,
        ReviewerConfig(model="reviewer-small", timeout_seconds=5),
    )

    result = adapter.review("A synthetic candidate.", context)

    assert result.status is ReviewStatus.COMPLETED
    assert result.verdict is ReviewVerdict.PASS
    assert result.scores.mode_compliance == 98
    assert transport.requests[0]["world_facts"][0]["fact_id"] == "fact.synthetic"
    assert "private_behavior" not in transport.requests[0]


def test_public_schema_rejects_unknown_fields_and_out_of_range_scores() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "reply_review.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    valid = {
        "schema_version": "p02.reply-review.v1",
        "status": "completed",
        "verdict": "rewrite",
        "violations": [
            {
                "code": "UNAUTHORIZED_SHARED_HISTORY",
                "severity": "hard",
                "evidence": {"start": 2, "end": 8},
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
    invalid_score = {**valid, "scores": {**valid["scores"], "mode_compliance": 101}}
    assert list(validator.iter_errors(invalid_score))


def test_invalid_provider_json_returns_a_sanitized_typed_failure() -> None:
    transport = _Transport(
        {
            "schema_version": "p02.reply-review.v1",
            "status": "completed",
            "verdict": "pass",
            "violations": [],
            "scores": {
                "persona_consistency": 100,
                "factual_consistency": 100,
                "relationship_boundary": 100,
                "mode_compliance": 101,
            },
        }
    )
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
    )

    result = JsonReviewerAdapter(transport, ReviewerConfig("reviewer-small")).review(
        "Synthetic candidate.", context
    )

    assert result.status is ReviewStatus.INVALID_RESPONSE
    assert result.verdict is ReviewVerdict.UNAVAILABLE
    assert result.error_code == "REVIEWER_RESPONSE_INVALID"
    assert result.violations == ()


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
        _FailingTransport(), ReviewerConfig("reviewer-small")
    ).review("Synthetic candidate.", context)

    assert disabled.status is ReviewStatus.DISABLED
    assert null_result.status is ReviewStatus.DISABLED
    assert transport.requests == []
    assert failed.status is ReviewStatus.UNAVAILABLE
    assert failed.error_code == "REVIEWER_UNAVAILABLE"
    assert "private" not in str(failed)


def test_reviewer_request_accepts_only_short_identified_reference_summaries() -> None:
    transport = _Transport(
        {
            "schema_version": "p02.reply-review.v1",
            "status": "completed",
            "verdict": "pass",
            "violations": [],
            "scores": {
                "persona_consistency": 100,
                "factual_consistency": 100,
                "relationship_boundary": 100,
                "mode_compliance": 100,
            },
        }
    )
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
    )

    JsonReviewerAdapter(transport, ReviewerConfig("reviewer-small")).review(
        "Synthetic candidate.",
        context,
        references=(ReviewReference("ref.synthetic", "Short synthetic summary."),),
    )

    assert transport.requests[0]["references"] == [
        {"reference_id": "ref.synthetic", "summary": "Short synthetic summary."}
    ]
    assert set(transport.requests[0]) == {
        "candidate",
        "mode",
        "output_constraints",
        "world_facts",
        "references",
    }
