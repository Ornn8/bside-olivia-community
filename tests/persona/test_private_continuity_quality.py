import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from llm_gateway import Gateway, GatewayResponse
from reply_context import (
    KnownContinuationFact,
    PrivateBehaviorView,
    ReplyContext,
    ReplyMode,
    TrustedTime,
)
from reply_model_quality import GatewayPersonaRewriter


ROOT = Path(__file__).resolve().parents[2]


class RecordingGateway(Gateway):
    stream_enabled = False

    def __init__(self) -> None:
        self.payload: dict[str, object] = {}

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        request_id: str | None = None,
    ) -> GatewayResponse:
        self.payload = json.loads(str(messages[-1]["content"]))
        return GatewayResponse(
            text="改写后的合成回复。",
            request_id=request_id or "synthetic",
            provider="synthetic",
            model="synthetic",
        )


def test_one_shot_rewriter_receives_only_character_known_continuations() -> None:
    gateway = RecordingGateway()
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
        private_behavior=PrivateBehaviorView(
            known_continuations=(
                KnownContinuationFact(
                    "class.known",
                    "她已经知道下周课程会调整。",
                ),
            )
        ),
    )
    rewriter = GatewayPersonaRewriter(
        gateway,
        ROOT / "linli_character" / "persona_release_v2.json",
        timeout_seconds=1,
    )

    result = rewriter.rewrite(
        "候选回复。",
        context,
        ("SYNTHETIC_VIOLATION",),
    )

    assert result == "改写后的合成回复。"
    assert gateway.payload["known_continuations"] == [
        {
            "fact_id": "class.known",
            "statement": "她已经知道下周课程会调整。",
        }
    ]
    serialized = repr(gateway.payload)
    assert "control_only" not in serialized
    assert "pending" not in serialized
    assert "trust" not in serialized
