"""Narrow candidate-to-canonical reply pipeline."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from persona_assembly import UntrustedFragment, assemble_persona
from persona_loader import load_persona
from reply_context import ReplyContext
from reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
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
        prepared = _prepare_generation_request(request, context, self.orchestrator)
        candidate = await self.orchestrator.run(prepared)
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


def _prepare_generation_request(
    request: object,
    context: ReplyContext,
    orchestrator: OrchestratorPort,
) -> object:
    """Attach Persona messages before provider generation when the local bridge is used."""

    if (
        not isinstance(request, ReplyRequest)
        or request.messages is not None
        or not isinstance(request.content, str)
        or not request.content.strip()
    ):
        return request

    bridge = getattr(orchestrator, "gateway", None)
    adapter = getattr(bridge, "adapter", None)
    config = getattr(adapter, "config", None)
    provider_name = str(getattr(config, "provider", "none")).strip().lower()
    if (
        adapter is None
        or not getattr(config, "persona_v2_enabled", False)
        or provider_name in {"", "none", "disabled", "unconfigured"}
    ):
        return request

    persona_path = getattr(adapter, "persona_v2_path", None)
    memory_builder = getattr(adapter, "memory_prompt_builder", None)
    memory_port = getattr(adapter, "memory_port", None)
    if persona_path is None or memory_builder is None:
        raise ValueError("persona generation boundary is unavailable")

    loaded = load_persona(persona_path)
    memory_context = memory_builder.build(
        request.content,
        max_chars=min(
            request.max_input_chars,
            int(getattr(memory_port, "context_max_chars", 2400)),
        ),
    )
    history = (
        (UntrustedFragment("memory.references", memory_context.text),)
        if memory_context.text
        else ()
    )
    messages = assemble_persona(
        loaded.snapshot,
        context,
        user_input=request.content,
        max_units=request.max_input_chars,
        history=history,
    ).to_messages()
    return replace(request, content=None, messages=messages)
