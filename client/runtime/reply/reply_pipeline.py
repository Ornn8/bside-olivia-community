"""Narrow candidate-to-canonical reply pipeline."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import hashlib
import json
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


class _RecallBudgetExceeded(RuntimeError):
    code = 'RECALL_CONTEXT_BUDGET_EXCEEDED'


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
        except _RecallBudgetExceeded as error:
            return PipelineResult(
                request.request_id if isinstance(request, ReplyRequest) else '',
                ReplyState.FAILED, error_code=error.code, retryable=False,
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
        if isinstance(prepared, ReplyRequest) and prepared.messages:
            from runtime.memory.recall_check import prepare_recall_messages
            adapter = getattr(getattr(self.orchestrator, 'gateway', None), 'adapter', None)
            gateway = getattr(adapter, 'gateway', None)
            messages = await prepare_recall_messages(prepared.messages, gateway,
                max_input_chars=prepared.max_input_chars, request_id=prepared.request_id)
            prepared = replace(prepared, messages=messages)
        if isinstance(prepared, ReplyRequest) and prepared.messages:
            from .fact_attribution import prepare_dialogue_messages
            prepared = replace(prepared, messages=prepare_dialogue_messages(
                prepared.messages, max_input_chars=prepared.max_input_chars))
        if generation_note and isinstance(prepared, ReplyRequest) and prepared.messages:
            # Finalize the delivery contract after history/recall projection.
            # The current user input stays last; evidence cannot become the
            # last instruction defining what the model is supposed to output.
            from .fact_attribution import finalize_reply_messages
            try:
                messages = finalize_reply_messages(prepared.messages, generation_note,
                                                   max_input_chars=original_budget)
            except ValueError:
                return PipelineResult(prepared.request_id, ReplyState.FAILED,
                                      error_code='INPUT_TOO_LONG', retryable=False)
            prepared = replace(prepared, messages=messages, max_input_chars=original_budget)
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
    if persona_path is None or memory_builder is None:
        raise ValueError("persona generation boundary is unavailable")

    loaded = load_persona(persona_path)
    if loaded.snapshot.status != "READY":
        raise _PersonaNotReadyError(_PERSONA_NOT_READY)
    messages, evidence = assemble_reply_messages(adapter, loaded.snapshot, context,
        request.content, max_input_chars=request.max_input_chars)
    return _PreparedGeneration(replace(request, content=None, messages=messages), evidence)


def assemble_reply_messages(adapter, snapshot, context, content, *, max_input_chars, user_input=None):
    """One memory/world assembly for every user-facing reply and media plan."""
    from .fact_attribution import prepare_dialogue_messages
    messages, evidence, assembly_limit = _assemble_reply_evidence(adapter, snapshot, context, content,
        max_input_chars=max_input_chars, user_input=user_input)
    return prepare_dialogue_messages(messages, max_input_chars=assembly_limit), evidence


def _assemble_reply_evidence(adapter, snapshot, context, content, *, max_input_chars, user_input=None):
    life = getattr(adapter, 'daily_life_fragments', None)
    recent = getattr(adapter, 'recent_letter_fragments', None)
    recent = recent(content) if callable(recent) else ()
    if callable(life):
        # Older adapters expose only content; the local adapter accepts this
        # request's frozen recent window instead of reading the mailbox again.
        from inspect import signature
        try:
            accepts_recent = 'recent_fragments' in signature(life).parameters
        except (TypeError, ValueError):
            accepts_recent = False
        life = life(content, recent_fragments=recent) if accepts_recent else life(content)
    else:
        life = ()
    # All modes retain the same minimum turn context, including when the
    # interpreter is disabled. Reserve the frozen current snapshot as well.
    reserve = 512
    for fragment in life:
        if fragment.fragment_id == 'linli.daily-life':
            try:
                current = json.loads(fragment.text).get('current')
                if isinstance(current, dict):
                    reserve += len(json.dumps(current, ensure_ascii=False).replace('<', r'\u003c').replace('>', r'\u003e')) + 100
            except (ValueError, AttributeError, TypeError):
                pass
    assembly_limit = max(1, max_input_chars - reserve)
    options = dict(snapshot=snapshot, context=context,
        user_input=content if user_input is None else user_input,
        max_units=assembly_limit, evidence_summaries=life,
        relationship_expression_enabled=snapshot.status == 'READY')
    limit = getattr(adapter, '_memory_context_limit', None)
    disabled = callable(limit) and limit() == 0
    from runtime.memory.history_continuity import plan_history_query, HistoryQuery
    exclusions = getattr(adapter, '_memory_source_exclusions', lambda: ())()
    query_plan = HistoryQuery(content) if disabled else plan_history_query(content, recent, excluded=exclusions)
    hint = query_plan.fragment()
    if hint is not None:
        recent = (*recent, hint)
    from runtime.reply.prompt_budget import PromptBudgetExceeded
    try:
        baseline = assemble_persona(history=recent, **options)
    except PromptBudgetExceeded as error:
        raise _RecallBudgetExceeded() from error
    available = max(0, assembly_limit - len(baseline.system_content) - len(baseline.user_content) - 256)
    if disabled:
        adapter._build_memory_prompt(content, max_chars=0)
        return baseline.to_messages(), TrustedReviewEvidence(), assembly_limit
    from runtime.diagnostics.recall_trace import begin, selection as record_selection
    begin(adapter.memory_prompt_builder, query_plan.mode)
    build_memory_prompt = getattr(adapter, "_build_memory_prompt", None)
    build_memory_prompt = build_memory_prompt if callable(build_memory_prompt) else adapter.memory_prompt_builder.build
    memory = build_memory_prompt(query_plan.query, max_chars=max(1, available))
    # One query, optionally grounded in delivered context. Capacity retries
    # only repack the same evidence; the archive tail is a bounded local read.
    from runtime.memory.memory_prompt import MemoryPromptBuilder
    from runtime.memory.memory_port import NullMemoryPort
    recall = getattr(memory, 'recall_result', None)
    renderer = MemoryPromptBuilder(NullMemoryPort(), conversation_memory=None,
        max_tokens=getattr(adapter.memory_prompt_builder, 'max_tokens', 300000),
        legacy_budget=available, conversation_budget=available)
    if recall is not None:
        from runtime.memory.history_continuity import add_history_tail
        recall = add_history_tail(adapter.memory_prompt_builder, recall, query_plan,
            now=context.trusted_time.instant, excluded=exclusions)
        from runtime.memory.recall_trace import deepen_recall
        builder = adapter.memory_prompt_builder
        if callable(getattr(builder, 'trace_sources', None)):
            exclusions = getattr(adapter, '_memory_source_exclusions', lambda: ())()
            recall = deepen_recall(builder, recall, query=content,
                source_ids=_life_source_ids(life), exclude_source_ids=exclusions)
        states = dict(recall.source_status)
        states['world'] = 'available' if context.world_state_available else 'unavailable'
        recall = replace(recall, source_status=tuple(states.items()))
        memory = renderer.render(recall, max_chars=available)
    while available > 0:
        if not getattr(memory, 'text', ''):
            break
        selection = _selected_history(memory)
        result = assemble_persona(history=(*selection.fragments, *recent), **options)
        included = result.budget_report.included_ids
        if 'history.memory.references' in included:
            if recall is not None:
                record_selection(recall, memory.references)
            return result.to_messages(), selection.trusted_evidence, assembly_limit
        available = available * 3 // 4
        if recall is None:
            break  # Legacy builders cannot be safely requeried during one generation.
        memory = renderer.render(recall, max_chars=available)
    if recall is not None:
        record_selection(recall, ())
        # Reserve failure/omission disclosure before optional reference blocks.
        # This is the same untrusted wrapper used by the persona assembler.
        minimum = renderer.minimum_recall_status(recall)
        payload = json.dumps({'untrusted': True, 'text': minimum}, ensure_ascii=False, separators=(',', ':'))
        payload = payload.replace('<', r'\u003c').replace('>', r'\u003e')
        disclosure = '<untrusted_history>\n' + payload + '\n</untrusted_history>\n'
        remaining = assembly_limit - len(disclosure)
        if remaining < 1:
            raise _RecallBudgetExceeded(_RecallBudgetExceeded.code)
        from runtime.reply.prompt_budget import PromptBudgetExceeded
        try:
            result = assemble_persona(history=recent, **{**options, 'max_units': remaining})
        except PromptBudgetExceeded as error:
            raise _RecallBudgetExceeded(_RecallBudgetExceeded.code) from error
        return ({'role': 'system', 'content': result.system_content + disclosure},
                {'role': 'user', 'content': result.user_content}), TrustedReviewEvidence(), assembly_limit
    return baseline.to_messages(), TrustedReviewEvidence(), assembly_limit


def _life_source_ids(fragments) -> tuple[str, ...]:
    """Read explicit event provenance from this request's already-frozen life view."""
    sources = []
    for fragment in fragments:
        if fragment.fragment_id != 'linli.daily-life':
            continue
        try:
            value = json.loads(fragment.text)
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        items = [value.get('current'), value.get('last_observation')]
        items.extend(value.get('previous_observations', []) if isinstance(value.get('previous_observations'), list) else [])
        items.extend(value.get('threads', []) if isinstance(value.get('threads'), list) else [])
        for item in items:
            source = item.get('source_id') if isinstance(item, dict) else None
            if isinstance(source, str) and source and not source.startswith('day:') and source not in sources:
                sources.append(source)
    return tuple(sources[:12])


def _selected_history(memory_context: object) -> _SelectedHistory:
    trusted_replies: list[TrustedCharacterReply] = []
    remaining = _CHARACTER_REPLY_HISTORY_LIMIT
    references = getattr(memory_context, "references", ())
    if isinstance(references, tuple):
        for reference in references:
            fragment = _character_reply_fragment(reference, remaining=remaining)
            if fragment is None:
                continue
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
        # Generation sees only the source-bearing group, including its paired
        # user claim and time. A naked duplicate can look like present self-report.
        # The bounded reviewer projection remains separate and is usable only
        # when the corresponding memory block survives final prompt assembly.
        memory_reference,
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
