"""Narrow candidate-to-canonical reply pipeline."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import hashlib
from typing import Any, Mapping, Protocol

from persona_assembly import UntrustedFragment, assemble_persona
from persona_loader import load_persona
from reply_model_quality import create_model_quality_ports
from runtime.reply.reply_context import ReplyContext
from reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
from runtime.reply.reply_quality_gate import (
    DeliveryRepairDisposition,
    ReviewerPort,
    RewriterPort,
    run_reply_quality_gate,
)
from runtime.reply.reply_reviewer import (
    NullReviewer,
    TrustedCharacterReply,
    TrustedReviewEvidence,
)
from runtime.memory.memory_port import CONVERSATION_MEMORY, MemoryRecord


_CHARACTER_REPLY_HISTORY_LIMIT = 1200
_CHARACTER_REPLY_PREFIX = "character_reply: "
_PERSONA_NOT_READY = "PERSONA_NOT_READY"


class _PersonaNotReadyError(RuntimeError):
    """Configured Letter generation cannot publish a non-ready Persona package."""


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
    delivery_repair_disposition: DeliveryRepairDisposition = (
        DeliveryRepairDisposition.NONE
    )


@dataclass(frozen=True)
class _PreparedGeneration:
    request: object
    trusted_evidence: TrustedReviewEvidence = TrustedReviewEvidence()


@dataclass(frozen=True)
class _SelectedHistory:
    fragments: tuple[UntrustedFragment, ...]
    trusted_evidence: TrustedReviewEvidence


class ReplyPipeline:
    def __init__(
        self,
        orchestrator: OrchestratorPort,
        *,
        reviewer: ReviewerPort,
        rewriter: RewriterPort,
        discover_runtime_ports: bool = True,
    ) -> None:
        self.orchestrator = orchestrator
        runtime_reviewer, runtime_rewriter = (
            create_model_quality_ports(orchestrator)
            if discover_runtime_ports
            else (None, None)
        )
        self.reviewer = (
            runtime_reviewer
            if isinstance(reviewer, NullReviewer)
            and runtime_reviewer is not None
            else reviewer
        )
        self.rewriter = (
            runtime_rewriter
            if isinstance(rewriter, UnavailableRewriter)
            and runtime_rewriter is not None
            else rewriter
        )

    async def run(self, request: object, context: ReplyContext) -> PipelineResult:
        if not isinstance(context, ReplyContext):
            raise TypeError("ReplyContext is required")
        try:
            preparation = _prepare_generation_request(
                request,
                context,
                self.orchestrator,
            )
        except _PersonaNotReadyError:
            return PipelineResult(
                request.request_id if isinstance(request, ReplyRequest) else "",
                ReplyState.FAILED,
                error_code=_PERSONA_NOT_READY,
                retryable=False,
            )
        prepared = preparation.request
        candidate = await self.orchestrator.run(prepared)
        if candidate.state is not ReplyState.COMPLETED:
            return PipelineResult(
                candidate.request_id,
                candidate.state,
                error_code=candidate.error_code,
                retryable=candidate.retryable,
            )
        gate = await asyncio.to_thread(
            run_reply_quality_gate,
            candidate.text,
            context,
            reviewer=self.reviewer,
            rewriter=self.rewriter,
            generation_messages=_generation_messages(prepared),
            trusted_evidence=preparation.trusted_evidence,
        )
        if not gate.accepted:
            repairable_text = (
                gate.text
                if gate.delivery_repair_disposition
                is DeliveryRepairDisposition.VIDEO_LENGTH
                else ""
            )
            return PipelineResult(
                candidate.request_id,
                ReplyState.FAILED,
                text=repairable_text,
                error_code=gate.error_code or "REPLY_QUALITY_BLOCKED",
                quality_status=gate.status.value,
                violation_codes=gate.violation_codes,
                reviewer_calls=gate.reviewer_calls,
                rewrite_calls=gate.rewrite_calls,
                delivery_repair_disposition=(
                    gate.delivery_repair_disposition
                ),
            )
        return PipelineResult(
            candidate.request_id,
            ReplyState.COMPLETED,
            text=gate.text,
            quality_status=gate.status.value,
            violation_codes=gate.violation_codes,
            reviewer_calls=gate.reviewer_calls,
            rewrite_calls=gate.rewrite_calls,
            delivery_repair_disposition=gate.delivery_repair_disposition,
        )


def _prepare_generation_request(
    request: object,
    context: ReplyContext,
    orchestrator: OrchestratorPort,
) -> _PreparedGeneration:
    """Attach Persona messages before provider generation when the local bridge is used."""

    if (
        not isinstance(request, ReplyRequest)
        or request.messages is not None
        or not isinstance(request.content, str)
        or not request.content.strip()
    ):
        return _PreparedGeneration(request)

    bridge = getattr(orchestrator, "gateway", None)
    adapter = getattr(bridge, "adapter", None)
    config = getattr(adapter, "config", None)
    provider_name = str(getattr(config, "provider", "none")).strip().lower()
    if (
        adapter is None
        or not getattr(config, "persona_v2_enabled", False)
        or provider_name in {"", "none", "disabled", "unconfigured"}
    ):
        return _PreparedGeneration(request)

    persona_path = getattr(adapter, "persona_v2_path", None)
    memory_builder = getattr(adapter, "memory_prompt_builder", None)
    memory_port = getattr(adapter, "memory_port", None)
    if persona_path is None or memory_builder is None:
        raise ValueError("persona generation boundary is unavailable")

    loaded = load_persona(persona_path)
    if loaded.snapshot.status != "READY":
        raise _PersonaNotReadyError(_PERSONA_NOT_READY)
    memory_limit = min(
        request.max_input_chars,
        int(getattr(memory_port, "context_max_chars", 2400)),
    )
    build_memory_prompt = getattr(adapter, "_build_memory_prompt", None)
    if callable(build_memory_prompt):
        memory_context = build_memory_prompt(
            request.content,
            max_chars=memory_limit,
        )
    else:
        # Test and third-party bridges may retain the original builder-only
        # surface; source selection is unavailable only outside the local
        # LetterAdapter production boundary.
        memory_context = memory_builder.build(
            request.content,
            max_chars=memory_limit,
        )
    selection = _selected_history(memory_context)
    messages = assemble_persona(
        loaded.snapshot,
        context,
        user_input=request.content,
        max_units=request.max_input_chars,
        history=selection.fragments,
    ).to_messages()
    return _PreparedGeneration(
        replace(request, content=None, messages=messages),
        selection.trusted_evidence,
    )


def _selected_history(memory_context: object) -> _SelectedHistory:
    character_replies: list[UntrustedFragment] = []
    trusted_replies: list[TrustedCharacterReply] = []
    remaining = _CHARACTER_REPLY_HISTORY_LIMIT
    references = getattr(memory_context, "references", ())
    if isinstance(references, tuple):
        for reference in references:
            fragment = _character_reply_fragment(reference, remaining=remaining)
            if fragment is None:
                continue
            character_replies.append(fragment)
            trusted_replies.append(
                TrustedCharacterReply(
                    fragment.fragment_id,
                    fragment.text[len(_CHARACTER_REPLY_PREFIX) :],
                )
            )
            remaining -= len(fragment.text)
            if remaining <= len(_CHARACTER_REPLY_PREFIX):
                break
    memory_text = getattr(memory_context, "text", "")
    memory_reference = (
        (UntrustedFragment("memory.references", memory_text),)
        if isinstance(memory_text, str) and memory_text
        else ()
    )
    return _SelectedHistory(
        (*character_replies, *memory_reference),
        TrustedReviewEvidence(tuple(trusted_replies)),
    )


def _character_reply_fragment(
    reference: object,
    *,
    remaining: int,
) -> UntrustedFragment | None:
    if not isinstance(reference, MemoryRecord):
        return None
    source_id = reference.provenance.get("source_record_id")
    if (
        reference.domain != CONVERSATION_MEMORY
        or not isinstance(source_id, str)
        or not source_id.startswith("history:")
        or reference.metadata.get("canonical") is not True
        or reference.metadata.get("history_actor") != "linli"
    ):
        return None
    text = reference.text.strip()
    available = remaining - len(_CHARACTER_REPLY_PREFIX)
    if (
        available <= 0
        or not text
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        return None
    digest = hashlib.sha256(
        f"{source_id}\0{reference.memory_id}".encode("utf-8")
    ).hexdigest()
    return UntrustedFragment(
        f"character_reply.{digest}",
        f"{_CHARACTER_REPLY_PREFIX}{text[:available]}",
    )


def _generation_messages(
    request: object,
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(request, ReplyRequest) or request.messages is None:
        return ()
    return tuple(dict(message) for message in request.messages)
