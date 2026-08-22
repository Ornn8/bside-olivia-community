from __future__ import annotations

from datetime import datetime, timezone
import json

from llm_gateway import GatewayConfig
from reply_context import ReplyContext, ReplyMode, TrustedTime
from reply_reviewer import JsonReviewerAdapter, ReviewerConfig, ReviewVerdict
from reply_runtime_models import OpenAICompatibleReviewTransport, RuntimeGatewayRewriter


class FakeResponse:
    def __init__(self, text: str) -> None:
        payload = {"choices": [{"message": {"content": text}}]}
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        return None


class CapturingOpener:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.requests: list[dict[str, object]] = []

    def __call__(self, request: object, *, timeout: float) -> FakeResponse:
        body = json.loads(getattr(request, "data").decode("utf-8"))
        self.requests.append({"body": body, "timeout": timeout})
        return FakeResponse(self.outputs.pop(0))


def _config() -> GatewayConfig:
    return GatewayConfig(
        provider="openai_compatible",
        base_url="https://synthetic.invalid/v1",
        model="synthetic-main",
        requires_api_key=False,
    )


def _context(mode: ReplyMode = ReplyMode.TEXT_LETTER) -> ReplyContext:
    return ReplyContext.create(
        mode,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
    )


def test_runtime_reviewer_returns_strict_result_without_private_behavior_payload() -> None:
    reviewer_json = json.dumps(
        {
            "schema_version": "p02.reply-review.v1",
            "status": "completed",
            "verdict": "pass",
            "violations": [],
            "scores": {
                "persona_consistency": 92,
                "factual_consistency": 100,
                "relationship_boundary": 100,
                "mode_compliance": 100,
            },
        }
    )
    opener = CapturingOpener([reviewer_json])
    reviewer = JsonReviewerAdapter(
        OpenAICompatibleReviewTransport(_config(), opener=opener),
        ReviewerConfig(model="synthetic-reviewer", timeout_seconds=3),
    )

    result = reviewer.review("我看到了。今天先不讲大道理。", _context())

    assert result.verdict is ReviewVerdict.PASS
    request_text = json.dumps(opener.requests[0]["body"], ensure_ascii=False)
    assert "synthetic-reviewer" in request_text
    assert "private_behavior" not in request_text
    assert "home_access" not in request_text
    assert "hidden_dimensions" not in request_text


def test_runtime_rewriter_minimally_repairs_candidate_with_mode_and_codes() -> None:
    opener = CapturingOpener(["改好后的最终正文。"])
    rewriter = RuntimeGatewayRewriter(_config(), opener=opener)

    text = rewriter.rewrite(
        "下面是三条建议：",
        _context(ReplyMode.SPOKEN_VIDEO),
        ("GENERIC_ASSISTANT_DRIFT",),
    )

    assert text == "改好后的最终正文。"
    body = opener.requests[0]["body"]
    messages = body["messages"]
    assert body["model"] == "synthetic-main"
    assert messages[0]["role"] == "system"
    repair = json.loads(messages[1]["content"])
    assert repair == {
        "mode": "spoken_video",
        "violation_codes": ["GENERIC_ASSISTANT_DRIFT"],
        "candidate": "下面是三条建议：",
    }


def test_reviewer_accepts_json_fences_but_rejects_non_contract_content() -> None:
    valid = {
        "schema_version": "p02.reply-review.v1",
        "status": "completed",
        "verdict": "pass",
        "violations": [],
        "scores": {
            "persona_consistency": 90,
            "factual_consistency": 90,
            "relationship_boundary": 90,
            "mode_compliance": 90,
        },
    }
    fenced = "```json\n" + json.dumps(valid) + "\n```"
    opener = CapturingOpener([fenced, "not-json"])
    transport = OpenAICompatibleReviewTransport(_config(), opener=opener)
    reviewer = JsonReviewerAdapter(
        transport,
        ReviewerConfig(model="synthetic-reviewer", timeout_seconds=3),
    )

    assert reviewer.review("合成正文", _context()).verdict is ReviewVerdict.PASS
    invalid = reviewer.review("另一段合成正文", _context())
    assert invalid.verdict is ReviewVerdict.UNAVAILABLE
    assert invalid.error_code == "REVIEWER_UNAVAILABLE"
