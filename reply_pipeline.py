"""Narrow candidate-to-canonical reply pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from reply_context import ReplyContext
from reply_orchestrator import ReplyResult, ReplyState
from reply_quality_gate import ReviewerPort, RewriterPort, run_reply_quality_gate


class OrchestratorPort(Protocol):
    async def run(self, request: object) -> ReplyResult: ...


class UnavailableRewriter:
    def rewrite(
        self,
        candidate: str,
        context: ReplyContext,
        violation_codes: tuple[str, ...],
    ) -> str:
        raise RuntimeError("rewriter is unavailable")


@dataclass(frozen=True)
class PipelineResult:
    request_id: str
    state: ReplyState
    text: str = ""
    error_code: str | None = None
    retryable: bool = False
    quality_status: str | None = None
    violation_codes: tuple[str, ...] = ()
    reviewer_calls: int = 0
    rewrite_calls: int = 0


class ReplyPipeline:
    def __init__(
        self,
        orchestrator: OrchestratorPort,
        *,
        reviewer: ReviewerPort,
        rewriter: RewriterPort,
    ) -> None:
        self.orchestrator = orchestrator
        self.reviewer = reviewer
        self.rewriter = rewriter

    async def run(self, request: object, context: ReplyContext) -> PipelineResult:
        if not isinstance(context, ReplyContext):
            raise TypeError("ReplyContext is required")
        candidate = await self.orchestrator.run(request)
        if candidate.state is not ReplyState.COMPLETED:
            return PipelineResult(
                candidate.request_id,
                candidate.state,
                error_code=candidate.error_code,
                retryable=candidate.retryable,
            )
        gate = run_reply_quality_gate(
            candidate.text,
            context,
            reviewer=self.reviewer,
            rewriter=self.rewriter,
        )
        if not gate.accepted:
            return PipelineResult(
                candidate.request_id,
                ReplyState.FAILED,
                error_code=gate.error_code or "REPLY_QUALITY_BLOCKED",
                quality_status=gate.status.value,
                violation_codes=gate.violation_codes,
                reviewer_calls=gate.reviewer_calls,
                rewrite_calls=gate.rewrite_calls,
            )
        return PipelineResult(
            candidate.request_id,
            ReplyState.COMPLETED,
            text=gate.text,
            quality_status=gate.status.value,
            violation_codes=gate.violation_codes,
            reviewer_calls=gate.reviewer_calls,
            rewrite_calls=gate.rewrite_calls,
        )

