"""Narrow candidate-to-canonical reply pipeline."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import hashlib
import os
from typing import Any, Mapping, Protocol

from persona_assembly import UntrustedFragment, assemble_persona
from persona_loader import load_persona
from reply_model_quality import create_model_quality_ports, resolve_model_quality_config
from runtime.reply.reply_context import ReplyContext, ReplyMode
from runtime.reply.current_turn_interpretation import (
    CurrentTurnInterpreter, projection_messages,
)
from reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
from runtime.reply.reply_quality_gate import (
    DeliveryRepairDisposition,
    ReviewerPort,
    RewriterPort,
)
from runtime.reply.reply_reviewer import (
    NullReviewer,
    TrustedCharacterReply,
    TrustedReviewEvidence,
)
from runtime.memory.memory_port import CONVERSATION_MEMORY, MemoryRecord
from runtime.letter_stickers.selection import allowed_stickers, selection_instruction, split_selection
from runtime.reply.letter_presentation import LETTER_PRESENTATION_INSTRUCTION, split_signature


_CHARACTER_REPLY_HISTORY_LIMIT = 1200
_CHARACTER_REPLY_PREFIX = "character_reply: "
_PERSONA_NOT_READY = "PERSONA_NOT_READY"


class _PersonaNotReadyError(RuntimeError):
    """Configured Letter generation cannot publish a non-ready Persona package."""


class OrchestratorPort(Protocol):
    async def run(self, request: object) -> ReplyResult: ...


class CurrentTurnInterpreterPort(Protocol):
    async def interpret(self, user_text: str) -> dict[str, Any]: ...


def current_turn_interpretation_enabled() -> bool:
    return os.environ.get("OLIVIA_LETTER_CURRENT_TURN_INTERPRETATION", "").strip().casefold() in {
        "1", "true", "yes", "on",
    }


def _runtime_current_turn_interpreter(orchestrator: object) -> CurrentTurnInterpreter | None:
    if not current_turn_interpretation_enabled():
        return None
    bridge = getattr(orchestrator, "gateway", None)
    adapter = getattr(bridge, "adapter", None)
    gateway = getattr(adapter, "gateway", None)
    if gateway is None:
        raise RuntimeError("CURRENT_TURN_INTERPRETATION_UNAVAILABLE")
    config = resolve_model_quality_config(getattr(adapter, "config", None))
    return CurrentTurnInterpreter(
        gateway, timeout_seconds=config.reasoning_timeout_seconds or config.timeout_seconds,
    )


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
    sticker_id: str | None = None
    signature: str | None = None
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
        current_turn_interpreter: CurrentTurnInterpreterPort | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.current_turn_interpreter = current_turn_interpreter or (
            _runtime_current_turn_interpreter(orchestrator) if discover_runtime_ports else None
        )
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
        sticker_choices = allowed_stickers(context.private_behavior)
        sticker_note = (LETTER_PRESENTATION_INSTRUCTION + '\n' + selection_instruction(sticker_choices)) if context.mode is ReplyMode.TEXT_LETTER else ""
        generation_note = sticker_note
        if context.mode is ReplyMode.FUTURE_IM:
            from runtime.personal_chat.presentation import CURRENT, INSTRUCTION
            if CURRENT.get() is not None:
                if CURRENT.get().get('structured'):
                    from runtime.personal_chat.decision import INSTRUCTION as DECISION_INSTRUCTION
                    generation_note = DECISION_INSTRUCTION
                else:
                    generation_note = INSTRUCTION
        original_budget = request.max_input_chars if isinstance(request, ReplyRequest) else 0
        generation_request = request
        if generation_note and isinstance(request, ReplyRequest) and request.messages is None and original_budget > len(generation_note) + 1000:
            generation_request = replace(request, max_input_chars=original_budget-len(generation_note)-2)
        try:
            preparation = _prepare_generation_request(
                generation_request,
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
        if self.current_turn_interpreter is not None and context.mode is ReplyMode.TEXT_LETTER:
            try:
                if not isinstance(prepared, ReplyRequest) or not isinstance(request, ReplyRequest) or not isinstance(request.content, str):
                    raise ValueError("current user input unavailable")
                interpretation = await self.current_turn_interpreter.interpret(request.content)
                messages = projection_messages(
                    _generation_messages(prepared), request.content, interpretation,
                )
                if sum(len(str(message.get("content", ""))) for message in messages) > prepared.max_input_chars:
                    raise ValueError("interpretation exceeds request budget")
                prepared = replace(prepared, messages=messages)
            except Exception:
                return PipelineResult(
                    request.request_id if isinstance(request, ReplyRequest) else "",
                    ReplyState.FAILED, error_code="CURRENT_TURN_INTERPRETATION_FAILED",
                )
        if generation_note and isinstance(prepared, ReplyRequest) and prepared.messages:
            messages = [dict(message) for message in prepared.messages]
            system = next((m for m in messages if m.get("role") == "system"), None)
            if system is None:
                messages.insert(0, {"role": "system", "content": generation_note})
            else:
                system["content"] += "\n\n" + generation_note
            if sum(len(str(m.get("content", ""))) for m in messages) <= original_budget:
                prepared = replace(prepared, messages=tuple(messages), max_input_chars=original_budget)
        candidate = await self.orchestrator.run(prepared)
        if candidate.state is not ReplyState.COMPLETED:
            return PipelineResult(
                candidate.request_id,
                candidate.state,
                error_code=candidate.error_code,
                retryable=candidate.retryable,
            )
        clean_text, sticker_id = split_selection(candidate.text, sticker_choices) if sticker_note else (candidate.text, None)
        clean_text, signature = split_signature(clean_text) if sticker_note else (clean_text, None)
        if not clean_text.strip():
            return PipelineResult(candidate.request_id, ReplyState.FAILED, error_code="PROVIDER_PROTOCOL")
        return PipelineResult(
            candidate.request_id,
            ReplyState.COMPLETED,
            text=clean_text,
            sticker_id=sticker_id,
            signature=signature,
            quality_status="not_checked",
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
    life_fragments = getattr(adapter, "daily_life_fragments", None)
    recent_fragments = getattr(adapter, "recent_letter_fragments", None)
    messages = assemble_persona(
        loaded.snapshot,
        context,
        user_input=request.content,
        max_units=request.max_input_chars,
        history=(*selection.fragments, *(recent_fragments(request.content) if callable(recent_fragments) else ())),
        evidence_summaries=life_fragments(request.content) if callable(life_fragments) else (),
        relationship_expression_enabled=True,
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
        or len(text) > available
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        return None
    digest = hashlib.sha256(
        f"{source_id}\0{reference.memory_id}".encode("utf-8")
    ).hexdigest()
    return UntrustedFragment(
        f"character_reply.{digest}",
        f"{_CHARACTER_REPLY_PREFIX}{text}",
    )


def _generation_messages(
    request: object,
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(request, ReplyRequest) or request.messages is None:
        return ()
    return tuple(dict(message) for message in request.messages)
