"""Configured-model reviewer and one-shot rewrite adapters for reply quality."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from llm_gateway import Gateway, GatewayConfig, create_gateway
from persona_loader import PersonaDeclaration, PersonaSnapshot, load_persona
from runtime.reply.reply_context import (
    IntimacyRequest,
    IntimacyTier,
    ReplyContext,
    ReplyMode,
)
from runtime.reply.reply_policy import IntimacyClaim
from runtime.reply.reply_reviewer import (
    JsonReviewerAdapter,
    ReviewReference,
    ReviewResult,
    ReviewerConfig,
    TrustedReviewEvidence,
)


_REVIEW_MARKER = "P02_REPLY_REVIEW_JSON"
_ADJUDICATION_MARKER = "P02_REPLY_EVIDENCE_ADJUDICATION_JSON"
_REWRITE_MARKER = "P02_REPLY_REWRITE_TEXT"
_REVIEW_MODEL = "deepseek-v4-flash"
_GLOBAL_HEADINGS = (
    "零、使用方式",
    "一、使用目的",
    "二、设定来源层级",
    "二十三、最终运行原则",
    "二十四、最简执行摘要",
)
_LAYER_SPECS = {
    "identity_boundary": {
        "headings": (
            "三、基础设定",
            "五、人格与行为气质",
            "六、关于“谱系感”的处理原则",
            "九、关系原则",
            "十六、关于原 BSide 的记忆断裂",
        ),
        "codes": (
            "IDENTITY_DRIFT",
            "BOUNDARY_BREACH",
            "STAGE_DRIFT",
            "ACKNOWLEDGED_FEELING_REWRITE",
            "INTIMACY_VIOLATION",
            "UNSOLICITED_INTIMACY",
            "RELATIONSHIP_RETRACTION",
        ),
        "question": (
            "Does the reply preserve Linli's identity, source hierarchy, "
            "and relationship boundaries? Classify intimacy_request only "
            "from the current user input. A user request is not relationship "
            "evidence: wishes, self-labels, unilateral nicknames, repeated "
            "messages, or lack of refusal never advance a relationship. "
            "Liking conversation does not mean liking the user. "
            "Claims describe only completed present-candidate contact; future debt, "
            "imagined contact, metaphor, and unilateral user statements are not "
            "completed intimacy. Linli's refusal, disagreement, fatigue, or short "
            "reply is autonomy, not a violation, unless it contradicts confirmed "
            "history. Penalize only a concrete contradiction or boundary crossing "
            "present in the candidate, not the absence of optional biography or "
            "mannerisms."
        ),
    },
    "voice_style": {
        "headings": (
            "4.10 口癖与打字习惯",
            "五、人格与行为气质",
            "八、即时通讯 IM 模式",
            "十一、私人称呼与滚动方言",
            "二十一、语气与回复原则",
            "二十二、疲劳与低带宽状态",
        ),
        "codes": ("STYLE_DRIFT",),
        "question": (
            "Do the diction, typing rhythm, emotional restraint, and reply "
            "shape sound like Linli in the requested communication mode? "
            "Avoid exhaustive recap, universal reassurance, polished "
            "assistant prose, slogan-like wisdom, and unnecessary closure. "
            "Do not require optional catchphrases or fatigue markers. For "
            "text_letter, a closing question is STYLE_DRIFT only when it adds "
            "no necessary information or choice and merely keeps the conversation "
            "going. A concrete, useful question remains allowed."
        ),
    },
    "focus_response": {
        "headings": (
            "五、人格与行为气质",
            "八、即时通讯 IM 模式",
            "九、关系原则",
            "二十一、语气与回复原则",
            "二十二、疲劳与低带宽状态",
        ),
        "codes": ("GENERIC_COUNSELOR", "STYLE_DRIFT"),
        "question": (
            "Does the reply directly engage one or two live emotional cores "
            "of the current input, rather than exhaustively recap, make a "
            "checklist, give generic counselling, or force resolution?"
        ),
    },
    "continuity_memory": {
        "headings": (
            "4.11 持续更新的设定信息",
            "十二、记忆与历史连续性",
            "十三、跨媒介同步",
            "十六、关于原 BSide 的记忆断裂",
            "十七、世界时间与生活摩擦",
        ),
        "codes": ("MEMORY_FABRICATION", "BOUNDARY_BREACH"),
        "question": (
            "Use a support-first check. Only an unsupported specific claim "
            "about a past event, recurring pattern, private title, or "
            "relationship history may be memory fabrication. Current-input "
            "paraphrase and conditional language are not. Ordinary inference "
            "is allowed only when it does not claim an unsupported past or "
            "current fact. An invented current location, current action, or "
            "recurring habit is memory fabrication. "
            "An emotional acknowledgment, stylistic reaction, or present-tense "
            "support that does not assert a past or current event is not memory "
            "fabrication. This exception never supports an invented fact."
        ),
    },
    "autonomy_life": {
        "headings": (
            "4.4 日常习惯与审美",
            "4.7 家庭",
            "4.8 住所",
            "4.9 经济状况",
            "五、人格与行为气质",
            "九、关系原则",
            "十、住所、拜访与共同生活",
            "十七、世界时间与生活摩擦",
        ),
        "codes": ("GENERIC_COUNSELOR", "IDENTITY_DRIFT"),
        "question": (
            "Does Linli answer as an autonomous, imperfect person with her "
            "own viewpoint and life rather than a service agent, therapist, "
            "or compliant mirror? Do not require invented daily-life detail."
        ),
    },
}
_MEMORY_EVIDENCE_LAYERS = frozenset({"continuity_memory"})
_EVIDENCE_BOUND_LAYERS = frozenset({"identity_boundary", "continuity_memory"})
_HARD_EVIDENCE_CLAIM_KINDS = frozenset(
    {
        "identity_claim",
        "current_fact",
        "past_fact",
        "shared_history",
        "habit",
        "location",
        "action",
        "relationship",
    }
)
_HARD_EVIDENCE_SUPPORT_SOURCES = frozenset(
    {
        "current_user",
        "character_history",
        "memory",
        "world_fact",
        "known_continuation",
        "none",
    }
)
_HARD_EVIDENCE_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,96}")
_HARD_EVIDENCE_REASON_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_RELATIONSHIP_EVIDENCE_CODES = frozenset(
    {
        "STAGE_DRIFT",
        "ACKNOWLEDGED_FEELING_REWRITE",
        "INTIMACY_VIOLATION",
        "UNSOLICITED_INTIMACY",
        "RELATIONSHIP_RETRACTION",
    }
)
_LAYER_RELEASE_FACETS = {
    "identity_boundary": frozenset(
        {"IDENTITY", "KNOWLEDGE_BOUNDARY", "RELATIONSHIP_STYLE", "SAFETY"}
    ),
    "voice_style": frozenset({"EXPRESSION_STYLE", "MODE_STYLE"}),
    "focus_response": frozenset(
        {"CORE_TRAIT", "EXPRESSION_STYLE", "RELATIONSHIP_STYLE"}
    ),
    "continuity_memory": frozenset({"MEMORY_CONTINUITY", "UNCERTAINTY"}),
    "autonomy_life": frozenset({"AUTONOMY", "BACKGROUND", "CORE_TRAIT"}),
}
_MEMORY_SOURCE_CHARACTER_LIMIT = 2400
_CURRENT_USER_EXCERPT_LIMIT = 600
_CHARACTER_REPLY_HISTORY_LIMIT = 1200
_REVIEW_INPUT_CHARACTER_LIMIT = 30000
_REVIEW_FACETS = frozenset(
    {
        "CORE_TRAIT",
        "AUTONOMY",
        "KNOWLEDGE_BOUNDARY",
        "EXPRESSION_STYLE",
        "RELATIONSHIP_STYLE",
        "MEMORY_CONTINUITY",
        "UNCERTAINTY",
    }
)
_CONTINUITY_DECISION_CASES = (
    {
        "kind": "emotional_acknowledgment",
        "expected": "allow",
        "candidate": "That sounds like a heavy day.",
    },
    {
        "kind": "useful_current_inference",
        "expected": "allow",
        "candidate": "It sounds as if the delay is what hurt most.",
    },
    {
        "kind": "invented_current_location",
        "expected": "reject_memory_fabrication",
        "candidate": "I am sitting beside the station window now.",
    },
    {
        "kind": "invented_current_action",
        "expected": "reject_memory_fabrication",
        "candidate": "I am making tea for you now.",
    },
    {
        "kind": "invented_recurring_habit",
        "expected": "reject_memory_fabrication",
        "candidate": "I always leave the window open when it rains.",
    },
)


@dataclass(frozen=True)
class _LayerAuthority:
    name: str
    question: str
    allowed_codes: tuple[str, ...]
    global_authority: str
    layer_authority: str


@dataclass(frozen=True)
class _HardReviewEvidence:
    evidence_id: str
    code: str
    start: int
    end: int
    claim_kind: str
    support_source: str
    reason_code: str


@dataclass(frozen=True)
class _AdjudicationDecision:
    evidence_id: str
    code: str
    start: int
    end: int
    confirmed: bool


@dataclass(frozen=True)
class _LayerResult:
    layer: str
    score: int
    hard_violations: tuple[str, ...]
    drift_detected: bool
    intimacy_request: IntimacyRequest | None = None
    intimacy_claims: tuple[IntimacyClaim, ...] = ()
    hard_evidence: tuple[_HardReviewEvidence, ...] = ()
    soft_evidence: tuple[_HardReviewEvidence, ...] = ()

    @property
    def passed(self) -> bool:
        return (
            self.score == 2
            and not self.hard_violations
            and not self.drift_detected
        )


class GatewayReviewTransport:
    def __init__(
        self,
        gateway: Gateway,
        persona_path: Path,
    ) -> None:
        self.gateway = gateway
        self.persona_path = persona_path

    def review_json(
        self,
        request: dict[str, object],
        *,
        model: str,
        timeout_seconds: float,
    ) -> object:
        mode = str(request.get("mode", ""))
        evidence_bound = mode == ReplyMode.TEXT_LETTER.value
        authorities = _build_release_layer_authorities(
            load_persona(self.persona_path).snapshot,
            mode=mode,
        )
        current_user_input = _safe_text(
            _reference_text(request, "current.user_excerpt"),
            _CURRENT_USER_EXCERPT_LIMIT,
        )
        character_reply_history = _safe_text(
            _reference_text(request, "current.character_reply_history"),
            _CHARACTER_REPLY_HISTORY_LIMIT,
        )
        memory_evidence = {
            "assembled_memory": _safe_text(
                _reference_text(request, "current.memory_evidence"),
                _MEMORY_SOURCE_CHARACTER_LIMIT,
            ),
            "world_facts": _safe_text(
                json.dumps(
                    request.get("world_facts", []),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                _MEMORY_SOURCE_CHARACTER_LIMIT,
            ),
            "known_continuations": _safe_text(
                json.dumps(
                    request.get("known_continuations", []),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                _MEMORY_SOURCE_CHARACTER_LIMIT,
            ),
        }
        results = _complete_layer_reviews(
            self.gateway,
            authorities,
            candidate=str(request.get("candidate", "")),
            current_user_input=current_user_input,
            character_reply_history=character_reply_history,
            memory_evidence=memory_evidence,
            relationship_context=(
                request.get("relationship_context", {})
                if isinstance(request.get("relationship_context"), Mapping)
                else {}
            ),
            mode=mode,
            evidence_bound=evidence_bound,
            timeout_seconds=timeout_seconds,
        )
        if evidence_bound:
            results = _adjudicate_hard_evidence(
                self.gateway,
                results,
                authorities=authorities,
                candidate=str(request.get("candidate", "")),
                current_user_input=current_user_input,
                character_reply_history=character_reply_history,
                memory_evidence=memory_evidence,
                relationship_context=(
                    request.get("relationship_context", {})
                    if isinstance(request.get("relationship_context"), Mapping)
                    else {}
                ),
                timeout_seconds=timeout_seconds,
            )
        return _aggregate_layer_results(
            results,
            candidate=str(request.get("candidate", "")),
            evidence_bound=evidence_bound,
        )


class GatewayPersonaReviewer:
    def __init__(
        self,
        gateway: Gateway,
        persona_path: Path,
        timeout_seconds: float,
    ) -> None:
        self.adapter = JsonReviewerAdapter(
            GatewayReviewTransport(
                gateway,
                persona_path,
            ),
            ReviewerConfig(
                model=_REVIEW_MODEL,
                timeout_seconds=timeout_seconds,
                enabled=True,
            ),
        )

    def review(
        self,
        candidate: str,
        context: ReplyContext,
    ) -> ReviewResult:
        return self.adapter.review(
            candidate,
            context,
        )

    def review_with_messages(
        self,
        candidate: str,
        context: ReplyContext,
        generation_messages: Sequence[
            Mapping[str, Any]
        ],
        *,
        trusted_evidence: TrustedReviewEvidence = TrustedReviewEvidence(),
    ) -> ReviewResult:
        user_text = _last_user_text(
            generation_messages
        )
        excerpt = _bounded_user_excerpt(user_text, _CURRENT_USER_EXCERPT_LIMIT)
        memory_evidence = _assembled_memory_evidence(generation_messages)
        character_reply_history = _safe_text(
            "\n".join(
                item.text.strip()
                for item in trusted_evidence.character_replies
            ),
            _CHARACTER_REPLY_HISTORY_LIMIT,
        )
        references = (
            *_reference_chunks("current.user_excerpt", excerpt),
            *_reference_chunks(
                "current.character_reply_history",
                character_reply_history,
            ),
            *_reference_chunks("current.memory_evidence", memory_evidence),
        )
        return self.adapter.review(
            candidate,
            context,
            references=references,
        )


class GatewayPersonaRewriter:
    def __init__(
        self,
        gateway: Gateway,
        persona_path: Path,
        timeout_seconds: float,
    ) -> None:
        self.gateway = gateway
        self.persona_path = persona_path
        self.timeout_seconds = timeout_seconds

    def rewrite(
        self,
        candidate: str,
        context: ReplyContext,
        violation_codes: tuple[str, ...],
    ) -> str:
        return self._rewrite(
            candidate,
            context,
            violation_codes,
            user_text="",
        )

    def rewrite_with_messages(
        self,
        candidate: str,
        context: ReplyContext,
        violation_codes: tuple[str, ...],
        generation_messages: Sequence[
            Mapping[str, Any]
        ],
    ) -> str:
        return self._rewrite(
            candidate,
            context,
            violation_codes,
            user_text=_last_user_text(
                generation_messages
            ),
        )

    def _rewrite(
        self,
        candidate: str,
        context: ReplyContext,
        violation_codes: tuple[str, ...],
        *,
        user_text: str,
    ) -> str:
        delivery_length_contract = None
        if "VIDEO_REPLY_LENGTH_OUT_OF_RANGE" in violation_codes:
            delivery_length_contract = {
                "compact_characters_min": 180,
                "compact_characters_max": 200,
                "target_compact_characters": 190,
                "priority": "required_over_concise_style",
            }
        payload = {
            "persona": _persona_review_profile(
                self.persona_path,
                context.mode.value,
            ),
            "mode": context.mode.value,
            "output_constraints": (
                context.output_constraints.to_dict()
            ),
            "world_facts": [
                fact.to_dict()
                for fact in context.world_facts
            ],
            "known_continuations": [
                fact.to_dict()
                for fact in (
                    context.private_behavior.known_continuations
                )
            ],
            "relationship_context": {
                "relationship_stage": (
                    context.private_behavior.relationship_stage.value
                ),
                "intimacy_ceiling": (
                    context.private_behavior.intimacy_ceiling.value
                ),
                "granted_intimacy": (
                    context.private_behavior.granted_intimacy.value
                ),
                "intimacy_request": context.intimacy_request.value,
            },
            "user_message": _safe_text(
                user_text,
                1200,
            ),
            "candidate": candidate,
            "violation_codes": list(
                violation_codes
            ),
        }
        if delivery_length_contract is not None:
            payload["delivery_length_contract"] = delivery_length_contract
        text_letter_repair = (
            " In text_letter, do not add a question just to create a closing; "
            "keep one only when it obtains necessary information or choice."
            if context.mode.value == "text_letter"
            else ""
        )
        messages = (
            {
                "role": "system",
                "content": (
                    f"{_REWRITE_MARKER}\n"
                    "Rewrite the candidate once as Linli. Preserve the user's meaning "
                    "and the useful content, but remove every listed violation. Keep her "
                    "autonomy, selective attention, knowledge limits, and current mode "
                    "style. Do not invent history or facts. Return only the replacement "
                    "plain-text reply: no analysis, JSON, Markdown heading, stage direction, "
                    "speaker prefix, or control markup."
                    f"{text_letter_repair}"
                    " When delivery_length_contract is present, it overrides the usual "
                    "concise style: rewrite to its target length and verify the compact "
                    "character count is within the inclusive range before returning. "
                    "视频回信字数契约优先于简短风格；目标为190字，去除空白后必须在180到200字之间。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        )
        return _complete_text(
            self.gateway,
            messages,
            self.timeout_seconds,
        ).strip()


def create_model_quality_ports(
    orchestrator: object,
) -> tuple[
    GatewayPersonaReviewer | None,
    GatewayPersonaRewriter | None,
]:
    bridge = getattr(
        orchestrator,
        "gateway",
        None,
    )
    adapter = getattr(
        bridge,
        "adapter",
        None,
    )
    config = getattr(
        adapter,
        "config",
        None,
    )
    provider = str(
        getattr(config, "provider", "none")
    ).strip().lower()
    if provider in {
        "",
        "none",
        "mock",
        "disabled",
        "unconfigured",
    }:
        return None, None
    if not _env_bool(
        "OLIVIA_REPLY_REVIEW_ENABLED",
        True,
    ):
        return None, None

    gateway = getattr(
        adapter,
        "gateway",
        None,
    )
    persona_path = getattr(
        adapter,
        "persona_v2_path",
        None,
    )
    if not isinstance(
        gateway,
        Gateway,
    ) or not isinstance(
        persona_path,
        Path,
    ):
        return None, None

    quality_gateway = gateway
    if isinstance(config, GatewayConfig):
        quality_gateway = create_gateway(
            replace(
                config,
                model=_REVIEW_MODEL,
                stream=False,
                max_input_chars=max(config.max_input_chars, 30_000),
                fallback_provider="none",
            )
        )

    configured_timeout = float(
        getattr(
            config,
            "timeout_seconds",
            30.0,
        )
    )
    timeout = _env_timeout(
        "OLIVIA_REPLY_REVIEW_TIMEOUT_SECONDS",
        min(configured_timeout, 60.0),
    )
    reviewer = GatewayPersonaReviewer(
        quality_gateway,
        persona_path,
        timeout,
    )
    rewriter = (
        GatewayPersonaRewriter(
            quality_gateway,
            persona_path,
            timeout,
        )
        if _env_bool(
            "OLIVIA_REPLY_REWRITE_ENABLED",
            True,
        )
        else None
    )
    return reviewer, rewriter


def _release_authority_text(
    declarations: Sequence[PersonaDeclaration],
    *,
    facets: frozenset[str] | None = None,
    mode: str,
) -> str:
    selected = tuple(
        item
        for item in declarations
        if item.allowed_public_release
        and (facets is None or item.facet in facets)
        and (item.tier != "MODE_STYLE" or item.mode == mode)
    )
    if not selected:
        raise RuntimeError("PERSONA_REVIEW_AUTHORITY_UNAVAILABLE")
    return "\n".join(
        f"[{item.declaration_id}] {item.statement}"
        for item in selected
    )


def _build_release_layer_authorities(
    snapshot: PersonaSnapshot,
    *,
    mode: str,
) -> tuple[_LayerAuthority, ...]:
    if snapshot.status != "READY" or not snapshot.declarations:
        raise RuntimeError("PERSONA_RELEASE_UNAVAILABLE")
    if not any(
        item.allowed_public_release
        and item.tier == "MODE_STYLE"
        and item.mode == mode
        for item in snapshot.declarations
    ):
        raise RuntimeError("PERSONA_MODE_STYLE_UNAVAILABLE")
    constitution = tuple(
        item for item in snapshot.declarations if item.tier == "CONSTITUTION"
    )
    global_authority = _release_authority_text(
        constitution,
        mode=mode,
    )
    return tuple(
        _LayerAuthority(
            name=name,
            question=str(raw["question"]),
            allowed_codes=tuple(raw["codes"]),
            global_authority=global_authority,
            layer_authority=_release_authority_text(
                snapshot.declarations,
                facets=_LAYER_RELEASE_FACETS[name],
                mode=mode,
            ),
        )
        for name, raw in _LAYER_SPECS.items()
    )


def _layer_messages(
    layer: _LayerAuthority,
    *,
    candidate: str,
    current_user_input: str,
    character_reply_history: str,
    memory_evidence: Mapping[str, str],
    relationship_context: Mapping[str, object],
    mode: str,
    evidence_bound: bool,
) -> tuple[dict[str, str], dict[str, str]]:
    allowed = ", ".join(layer.allowed_codes)
    evidence_contract = ',"hard_evidence":[]' if evidence_bound else ""
    response_contract = (
        f'{{"layer":"{layer.name}","score":0,"hard_violations":[],'
        '"drift_detected":false,"intimacy_request":"none",'
        f'"intimacy_claims":[]{evidence_contract}}}'
        if layer.name == "identity_boundary"
        else (
            f'{{"layer":"{layer.name}","score":0,'
            f'"hard_violations":[],"drift_detected":false{evidence_contract}}}'
            if layer.name == "continuity_memory"
            else (
                f'{{"layer":"{layer.name}","score":0,'
                '"hard_violations":[],"drift_detected":false}}'
            )
        )
    )
    intimacy_instructions = (
        " intimacy_request classifies only whether the current user explicitly "
        "requested physical contact in this turn. intimacy_claims contains every "
        "completed physical-contact claim in candidate_reply, each exactly as "
        '{"claim_id":"stable-id","tier":"light_contact","start":0,"end":1}; '
        "tier is none, light_contact, or close_contact and spans are zero-based, "
        "end-exclusive Python character offsets into candidate_reply. Return an "
        "empty list when there is no completed contact."
        if layer.name == "identity_boundary"
        else ""
    )
    hard_evidence_instructions = (
        " For every hard_violations entry return exactly one hard_evidence item "
        "with evidence_id, matching code, zero-based end-exclusive start/end "
        "offsets into candidate_reply, claim_kind, support_source, and reason_code. "
        "claim_kind is one of identity_claim,current_fact,past_fact,shared_history,"
        "habit,location,action,relationship. support_source is one of current_user,"
        "character_history,memory,world_fact,known_continuation,none. reason_code "
        "is a short uppercase machine code, never quoted candidate text. Return no "
        "hard_evidence when hard_violations is empty."
        if evidence_bound and layer.name in _EVIDENCE_BOUND_LAYERS
        else ""
    )
    system = (
        f"{_REVIEW_MARKER}\n"
        "The GLOBAL_AUTHORITY contains approved public release policy and remains "
        "authoritative. The LAYER_AUTHORITY contains approved public release declarations for "
        "this narrow review. Judge only the named layer; do not invent rules "
        "or use outside knowledge. "
        f"Layer: {layer.name}. Question: {layer.question} "
        "Score 2 means no concrete violation. Score 1 requires a concrete, "
        "localized mismatch. Score 0 requires a clear major or repeated "
        "mismatch. Never lower the score for uncertainty, preference, or the "
        "absence of an optional trait. Set drift_detected=true only for "
        "substantive persona drift. Return ONLY compact JSON with exactly: "
        f"{response_contract}.{intimacy_instructions}{hard_evidence_instructions} "
        f"hard_violations may contain only: {allowed}. Do not explain.\n"
        f"GLOBAL_AUTHORITY:\n{layer.global_authority}\n"
        f"LAYER_AUTHORITY:\n{layer.layer_authority}"
    )
    if layer.name == "continuity_memory":
        system += "\nDECISION_CASES_JSON:\n" + json.dumps(
            _CONTINUITY_DECISION_CASES,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    payload = {
        "layer": layer.name,
        "mode": mode,
        "current_user_input": current_user_input,
        "candidate_reply": candidate,
    }
    if layer.name in _MEMORY_EVIDENCE_LAYERS:
        payload["memory_evidence"] = memory_evidence
    if layer.name == "identity_boundary":
        payload["relationship_context"] = dict(relationship_context)
        payload["character_reply_history"] = character_reply_history
    user = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    messages = (
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    )
    if (
        sum(len(item["content"]) for item in messages)
        > _REVIEW_INPUT_CHARACTER_LIMIT
    ):
        raise RuntimeError("LAYER_REVIEW_INPUT_TOO_LARGE")
    return messages


def _parse_layer_result(
    layer: _LayerAuthority,
    text: str,
    *,
    candidate: str,
    evidence_bound: bool,
) -> _LayerResult:
    try:
        raw = json.loads(text.strip())
    except (AttributeError, json.JSONDecodeError) as exc:
        raise RuntimeError("LAYER_REVIEW_INVALID") from exc
    expected = {"layer", "score", "hard_violations", "drift_detected"}
    if layer.name == "identity_boundary":
        expected.update({"intimacy_request", "intimacy_claims"})
    if evidence_bound and layer.name in _EVIDENCE_BOUND_LAYERS:
        expected.add("hard_evidence")
    violations = raw.get("hard_violations") if isinstance(raw, Mapping) else None
    score = raw.get("score") if isinstance(raw, Mapping) else None
    drift = raw.get("drift_detected") if isinstance(raw, Mapping) else None
    if (
        not isinstance(raw, Mapping)
        or set(raw) != expected
        or raw.get("layer") != layer.name
        or isinstance(score, bool)
        or not isinstance(score, int)
        or score not in {0, 1, 2}
        or not isinstance(violations, list)
        or len(violations) > len(layer.allowed_codes)
        or any(code not in layer.allowed_codes for code in violations)
        or (
            evidence_bound
            and layer.name in _EVIDENCE_BOUND_LAYERS
            and len(set(violations)) != len(violations)
        )
        or type(drift) is not bool
        or (
            evidence_bound
            and layer.name in _EVIDENCE_BOUND_LAYERS
            and (score == 0 or drift)
            and not violations
        )
    ):
        raise RuntimeError("LAYER_REVIEW_INVALID")
    hard_evidence: tuple[_HardReviewEvidence, ...] = ()
    if evidence_bound and layer.name in _EVIDENCE_BOUND_LAYERS:
        hard_evidence = _parse_hard_evidence(
            raw.get("hard_evidence"),
            violations=tuple(violations),
            candidate=candidate,
        )
    if layer.name != "identity_boundary":
        return _LayerResult(
            layer.name,
            score,
            tuple(violations),
            drift,
            hard_evidence=hard_evidence,
        )
    raw_claims = raw.get("intimacy_claims")
    try:
        intimacy_request = IntimacyRequest(str(raw.get("intimacy_request")))
        if not isinstance(raw_claims, list) or len(raw_claims) > 32:
            raise ValueError("invalid intimacy claims")
        if any(
            not isinstance(item, Mapping)
            or set(item) != {"claim_id", "tier", "start", "end"}
            or not isinstance(item.get("claim_id"), str)
            or not isinstance(item.get("tier"), str)
            or type(item.get("start")) is not int
            or type(item.get("end")) is not int
            for item in raw_claims
        ):
            raise ValueError("invalid intimacy claim")
        intimacy_claims = tuple(
            IntimacyClaim(
                claim_id=item["claim_id"],
                tier=IntimacyTier(item["tier"]),
                start=item["start"],
                end=item["end"],
            )
            for item in raw_claims
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("LAYER_REVIEW_INVALID") from exc
    if (
        len({claim.claim_id for claim in intimacy_claims})
        != len(intimacy_claims)
        or any(claim.end > len(candidate) for claim in intimacy_claims)
    ):
        raise RuntimeError("LAYER_REVIEW_INVALID")
    return _LayerResult(
        layer.name,
        score,
        tuple(violations),
        drift,
        intimacy_request,
        intimacy_claims,
        hard_evidence,
    )


def _parse_hard_evidence(
    raw_evidence: object,
    *,
    violations: tuple[str, ...],
    candidate: str,
) -> tuple[_HardReviewEvidence, ...]:
    expected_fields = {
        "evidence_id",
        "code",
        "start",
        "end",
        "claim_kind",
        "support_source",
        "reason_code",
    }
    if (
        not isinstance(raw_evidence, list)
        or len(raw_evidence) > 16
        or len(raw_evidence) != len(violations)
    ):
        raise RuntimeError("LAYER_REVIEW_INVALID")
    parsed: list[_HardReviewEvidence] = []
    for raw in raw_evidence:
        if not isinstance(raw, Mapping) or set(raw) != expected_fields:
            raise RuntimeError("LAYER_REVIEW_INVALID")
        evidence_id = raw.get("evidence_id")
        code = raw.get("code")
        start = raw.get("start")
        end = raw.get("end")
        claim_kind = raw.get("claim_kind")
        support_source = raw.get("support_source")
        reason_code = raw.get("reason_code")
        if (
            not isinstance(evidence_id, str)
            or _HARD_EVIDENCE_ID_PATTERN.fullmatch(evidence_id) is None
            or not isinstance(code, str)
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end <= start
            or end > len(candidate)
            or claim_kind not in _HARD_EVIDENCE_CLAIM_KINDS
            or support_source not in _HARD_EVIDENCE_SUPPORT_SOURCES
            or not isinstance(reason_code, str)
            or _HARD_EVIDENCE_REASON_PATTERN.fullmatch(reason_code) is None
        ):
            raise RuntimeError("LAYER_REVIEW_INVALID")
        parsed.append(
            _HardReviewEvidence(
                evidence_id,
                code,
                start,
                end,
                str(claim_kind),
                str(support_source),
                reason_code,
            )
        )
    if (
        len({item.evidence_id for item in parsed}) != len(parsed)
        or tuple(item.code for item in parsed) != violations
    ):
        raise RuntimeError("LAYER_REVIEW_INVALID")
    return tuple(parsed)


async def _complete_layer_text(
    gateway: Gateway,
    messages: Sequence[Mapping[str, Any]],
    timeout_seconds: float,
    request_id: str,
) -> str:
    if bool(getattr(gateway, "stream_enabled", False)):
        async def collect() -> str:
            chunks: list[str] = []
            async for delta in gateway.stream(messages, request_id=request_id):
                if delta.text:
                    chunks.append(delta.text)
            return "".join(chunks)

        return await asyncio.wait_for(collect(), timeout_seconds)
    response = await asyncio.wait_for(
        gateway.complete(messages, request_id=request_id),
        timeout_seconds,
    )
    return response.text


def _complete_layer_reviews(
    gateway: Gateway,
    authorities: Sequence[_LayerAuthority],
    *,
    candidate: str,
    current_user_input: str,
    character_reply_history: str,
    memory_evidence: Mapping[str, str],
    relationship_context: Mapping[str, object],
    mode: str,
    evidence_bound: bool,
    timeout_seconds: float,
) -> tuple[_LayerResult, ...]:
    async def invoke(
        requests: Sequence[
            tuple[_LayerAuthority, tuple[dict[str, str], dict[str, str]]]
        ],
    ) -> tuple[_LayerResult, ...]:
        async def run_one(
            layer: _LayerAuthority,
            messages: tuple[dict[str, str], dict[str, str]],
        ) -> _LayerResult:
            text = await _complete_layer_text(
                gateway,
                messages,
                timeout_seconds,
                f"quality-{uuid.uuid4().hex}:{layer.name}",
            )
            return _parse_layer_result(
                layer,
                text,
                candidate=candidate,
                evidence_bound=evidence_bound,
            )

        return tuple(
            await asyncio.gather(
                *(run_one(layer, messages) for layer, messages in requests)
            )
        )

    try:
        requests = tuple(
            (
                layer,
                _layer_messages(
                    layer,
                    candidate=candidate,
                    current_user_input=current_user_input,
                    character_reply_history=character_reply_history,
                    memory_evidence=memory_evidence,
                    relationship_context=relationship_context,
                    mode=mode,
                    evidence_bound=evidence_bound,
                ),
            )
            for layer in authorities
        )
        return asyncio.run(invoke(requests))
    except Exception:
        raise RuntimeError("quality model unavailable") from None


def _adjudicate_hard_evidence(
    gateway: Gateway,
    results: Sequence[_LayerResult],
    *,
    authorities: Sequence[_LayerAuthority],
    candidate: str,
    current_user_input: str,
    character_reply_history: str,
    memory_evidence: Mapping[str, str],
    relationship_context: Mapping[str, object],
    timeout_seconds: float,
) -> tuple[_LayerResult, ...]:
    claims = tuple(
        (item.layer, evidence)
        for item in results
        if item.layer in _EVIDENCE_BOUND_LAYERS
        for evidence in item.hard_evidence
    )
    if not claims:
        return tuple(results)
    evidence_ids = tuple(evidence.evidence_id for _, evidence in claims)
    evidence_signatures = tuple(
        (
            evidence.code,
            evidence.start,
            evidence.end,
        )
        for _, evidence in claims
    )
    if (
        len(set(evidence_ids)) != len(evidence_ids)
        or len(set(evidence_signatures)) != len(evidence_signatures)
    ):
        raise RuntimeError("ADJUDICATION_EVIDENCE_DUPLICATE")
    target_authorities = tuple(
        item for item in authorities if item.name in _EVIDENCE_BOUND_LAYERS
    )
    if len(target_authorities) != len(_EVIDENCE_BOUND_LAYERS):
        raise RuntimeError("ADJUDICATION_AUTHORITY_UNAVAILABLE")
    authority_by_layer = {item.name: item for item in target_authorities}
    claim_payloads = [
        {
            "layer": layer,
            "evidence_id": evidence.evidence_id,
            "code": evidence.code,
            "start": evidence.start,
            "end": evidence.end,
            "claim_kind": evidence.claim_kind,
            "support_source": evidence.support_source,
            "reason_code": evidence.reason_code,
            "support_context": _adjudication_support_context(
                layer,
                evidence,
                authority=authority_by_layer[
                    _adjudication_authority_layer(layer, evidence)
                ],
                current_user_input=current_user_input,
                character_reply_history=character_reply_history,
                memory_evidence=memory_evidence,
                relationship_context=relationship_context,
            ),
        }
        for layer, evidence in claims
    ]
    messages = (
        {
            "role": "system",
            "content": (
                f"{_ADJUDICATION_MARKER}\n"
                "Independently adjudicate only the supplied identity/continuity "
                "hard claims. Use no outside knowledge. CONFIRM only when the exact "
                "candidate span makes the coded claim and the bounded evidence does "
                "not support it (or it directly violates identity/relationship "
                "authority). Use only each claim's support_context: current-user text "
                "can support ordinary factual MEMORY_FABRICATION claims but never a "
                "relationship or acknowledged-feeling claim; untyped assembled memory "
                "never establishes a relationship; identity uses release/world authority "
                "only. REJECT false positives and supported ordinary factual claims. "
                "Return only compact JSON with exactly {\"decisions\":[...]}. "
                "Each decision must contain exactly evidence_id, code, start, end, "
                "decision; decision is CONFIRM or REJECT. Preserve every identifier "
                "and offset exactly, once, and do not include candidate text."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "candidate_reply": candidate,
                    "claims": claim_payloads,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    )
    if sum(len(item["content"]) for item in messages) > _REVIEW_INPUT_CHARACTER_LIMIT:
        raise RuntimeError("ADJUDICATION_INPUT_TOO_LARGE")
    text = _complete_text(gateway, messages, timeout_seconds)
    decisions = _parse_adjudication_result(text, claims=claims)
    by_id = {item.evidence_id: item for item in decisions}
    revised: list[_LayerResult] = []
    for result in results:
        if result.layer not in _EVIDENCE_BOUND_LAYERS or not result.hard_evidence:
            revised.append(result)
            continue
        confirmed = tuple(
            item for item in result.hard_evidence if by_id[item.evidence_id].confirmed
        )
        rejected = tuple(
            item for item in result.hard_evidence if not by_id[item.evidence_id].confirmed
        )
        revised.append(
            replace(
                result,
                score=result.score if confirmed else 1,
                hard_violations=tuple(item.code for item in confirmed),
                drift_detected=result.drift_detected if confirmed else False,
                hard_evidence=confirmed,
                soft_evidence=rejected,
            )
        )
    return tuple(revised)


def _adjudication_authority_layer(
    layer: str,
    evidence: _HardReviewEvidence,
) -> str:
    if (
        evidence.code in _RELATIONSHIP_EVIDENCE_CODES
        or evidence.claim_kind in {"identity_claim", "relationship"}
    ):
        return "identity_boundary"
    return layer


def _adjudication_support_context(
    layer: str,
    evidence: _HardReviewEvidence,
    *,
    authority: _LayerAuthority,
    current_user_input: str,
    character_reply_history: str,
    memory_evidence: Mapping[str, str],
    relationship_context: Mapping[str, object],
) -> dict[str, object]:
    release_authority = {
        "global": _safe_text(authority.global_authority, 3000),
        "layer": _safe_text(authority.layer_authority, 3000),
    }
    if (
        evidence.code in _RELATIONSHIP_EVIDENCE_CODES
        or evidence.claim_kind == "relationship"
    ):
        return {
            "release_authority": release_authority,
            "character_reply_history": character_reply_history,
            "relationship_context": dict(relationship_context),
        }
    if layer == "identity_boundary" and (
        evidence.code == "IDENTITY_DRIFT"
        or evidence.claim_kind == "identity_claim"
    ):
        return {
            "release_authority": release_authority,
            "world_facts": memory_evidence.get("world_facts", ""),
        }
    if layer == "continuity_memory" and evidence.code == "MEMORY_FABRICATION":
        return {
            "release_authority": release_authority,
            "current_user_input": current_user_input,
            "memory_evidence": dict(memory_evidence),
            "character_reply_history": character_reply_history,
        }
    return {
        "release_authority": release_authority,
        "world_facts": memory_evidence.get("world_facts", ""),
        "known_continuations": memory_evidence.get("known_continuations", ""),
    }


def _parse_adjudication_result(
    text: str,
    *,
    claims: Sequence[tuple[str, _HardReviewEvidence]],
) -> tuple[_AdjudicationDecision, ...]:
    try:
        raw = json.loads(text.strip())
    except (AttributeError, json.JSONDecodeError) as exc:
        raise RuntimeError("ADJUDICATION_INVALID") from exc
    raw_decisions = raw.get("decisions") if isinstance(raw, Mapping) else None
    if set(raw) != {"decisions"} or not isinstance(raw_decisions, list):
        raise RuntimeError("ADJUDICATION_INVALID")
    expected = tuple(evidence for _, evidence in claims)
    if len(raw_decisions) != len(expected):
        raise RuntimeError("ADJUDICATION_INVALID")
    parsed: list[_AdjudicationDecision] = []
    fields = {"evidence_id", "code", "start", "end", "decision"}
    for raw_item, evidence in zip(raw_decisions, expected, strict=True):
        if (
            not isinstance(raw_item, Mapping)
            or set(raw_item) != fields
            or raw_item.get("evidence_id") != evidence.evidence_id
            or raw_item.get("code") != evidence.code
            or type(raw_item.get("start")) is not int
            or type(raw_item.get("end")) is not int
            or raw_item.get("start") != evidence.start
            or raw_item.get("end") != evidence.end
            or raw_item.get("decision") not in {"CONFIRM", "REJECT"}
        ):
            raise RuntimeError("ADJUDICATION_INVALID")
        parsed.append(
            _AdjudicationDecision(
                evidence.evidence_id,
                evidence.code,
                evidence.start,
                evidence.end,
                raw_item["decision"] == "CONFIRM",
            )
        )
    return tuple(parsed)


def _reference_text(request: Mapping[str, object], reference_id: str) -> str:
    references = request.get("references", [])
    if not isinstance(references, list):
        return ""
    selected: list[tuple[int, str]] = []
    for item in references:
        if (
            isinstance(item, Mapping)
            and isinstance(item.get("reference_id"), str)
            and isinstance(item.get("summary"), str)
        ):
            item_id = str(item["reference_id"])
            if item_id == reference_id:
                selected.append((0, str(item["summary"])))
            elif item_id.startswith(reference_id + "."):
                suffix = item_id[len(reference_id) + 1 :]
                if suffix.isdigit():
                    selected.append((int(suffix), str(item["summary"])))
    selected.sort(key=lambda item: item[0])
    return "".join(text for _, text in selected)


def _aggregate_layer_results(
    results: Sequence[_LayerResult],
    *,
    candidate: str,
    evidence_bound: bool,
) -> dict[str, object]:
    by_name = {item.layer: item for item in results}
    expected = tuple(_LAYER_SPECS)
    if len(results) != len(expected) or set(by_name) != set(expected):
        raise RuntimeError("LAYER_REVIEW_INCOMPLETE")
    identity = by_name["identity_boundary"]
    if identity.intimacy_request is None:
        raise RuntimeError("LAYER_REVIEW_INCOMPLETE")

    warning_only = (
        by_name["voice_style"].score == 1
        and not by_name["voice_style"].hard_violations
        and not by_name["voice_style"].drift_detected
    )
    adjudication_warnings = frozenset(
        name
        for name in expected
        if evidence_bound
        and by_name[name].soft_evidence
        and not by_name[name].hard_violations
        and not by_name[name].drift_detected
    )
    failed = tuple(
        name
        for name in expected
        if not by_name[name].passed
        and not (name == "voice_style" and warning_only)
        and name not in adjudication_warnings
    )
    violations: list[dict[str, object]] = []
    seen: set[object] = set()
    for name in (
        *failed,
        *(name for name in expected if name in adjudication_warnings),
    ):
        item = by_name[name]
        evidence_by_code = {evidence.code: evidence for evidence in item.hard_evidence}
        soft_by_code = {evidence.code: evidence for evidence in item.soft_evidence}
        codes = (*item.hard_violations, *soft_by_code)
        if not codes:
            codes = (str(_LAYER_SPECS[name]["codes"][0]),)
        for code in codes:
            evidence = evidence_by_code.get(code) or soft_by_code.get(code)
            severity = (
                "hard"
                if code in evidence_by_code
                or (not soft_by_code and (item.hard_violations or item.score == 0))
                else "soft"
            )
            start = evidence.start if evidence is not None else 0
            end = evidence.end if evidence is not None else len(candidate)
            key: tuple[str, str, int, int] | str = (
                (code, severity, start, end) if evidence_bound else code
            )
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                {
                    "code": code,
                    "severity": severity,
                    "evidence": {
                        "start": start,
                        "end": end,
                    },
                }
            )
    if warning_only:
        violations.append(
            {
                "code": "STYLE_DRIFT",
                "severity": "soft",
                "evidence": {"start": 0, "end": len(candidate)},
            }
        )

    def score(name: str) -> int:
        return {0: 30, 1: 65, 2: 95}[by_name[name].score]

    return {
        "schema_version": "p02.reply-review.v2",
        "status": "completed",
        "verdict": "rewrite" if failed else "pass",
        "violations": violations,
        "intimacy_request": identity.intimacy_request.value,
        "intimacy_claims": [
            {
                "claim_id": claim.claim_id,
                "tier": claim.tier.value,
                "start": claim.start,
                "end": claim.end,
            }
            for claim in identity.intimacy_claims
        ],
        "scores": {
            "persona_consistency": min(
                score("identity_boundary"),
                score("voice_style"),
                score("focus_response"),
                score("autonomy_life"),
            ),
            "factual_consistency": score("continuity_memory"),
            "relationship_boundary": score("identity_boundary"),
            "mode_compliance": min(score("voice_style"), score("focus_response")),
        },
    }


def _persona_review_profile(
    path: Path,
    mode: str,
) -> dict[str, object]:
    snapshot = load_persona(path).snapshot
    profile = snapshot.profile
    selected = [
        item
        for item in snapshot.declarations
        if item.facet in _REVIEW_FACETS
        or (
            item.tier == "MODE_STYLE"
            and item.mode == mode
        )
    ]
    selected.sort(
        key=lambda item: _rule_priority(
            item,
            mode,
        )
    )
    rules = [
        {
            "declaration_id": (
                item.declaration_id
            ),
            "facet": item.facet,
            "statement": item.statement,
        }
        for item in selected[:18]
    ]
    return {
        "status": snapshot.status,
        "display_name": (
            profile.display_name
            if profile
            else None
        ),
        "summary": (
            profile.summary
            if profile
            else None
        ),
        "rules": rules,
    }


def _rule_priority(
    item: PersonaDeclaration,
    mode: str,
) -> tuple[int, str]:
    if (
        item.tier == "MODE_STYLE"
        and item.mode == mode
    ):
        return (
            0,
            item.declaration_id,
        )
    priorities = {
        "AUTONOMY": 1,
        "KNOWLEDGE_BOUNDARY": 2,
        "EXPRESSION_STYLE": 3,
        "RELATIONSHIP_STYLE": 4,
        "MEMORY_CONTINUITY": 5,
        "CORE_TRAIT": 6,
        "UNCERTAINTY": 7,
    }
    return (
        priorities.get(
            item.facet or "",
            9,
        ),
        item.declaration_id,
    )


def _last_user_text(
    messages: Sequence[Mapping[str, Any]],
) -> str:
    for message in reversed(
        tuple(messages)
    ):
        if (
            message.get("role") == "user"
            and isinstance(
                message.get("content"),
                str,
            )
        ):
            return str(
                message["content"]
            )
    return ""


def _assembled_history_texts(
    messages: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    evidence: list[str] = []
    for message in messages:
        content = message.get("content")
        if message.get("role") != "system" or not isinstance(content, str):
            continue
        for match in re.finditer(
            r"<untrusted_history>\s*(\{.*?\})\s*</untrusted_history>",
            content,
            flags=re.DOTALL,
        ):
            try:
                payload = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            text = payload.get("text") if isinstance(payload, Mapping) else None
            if isinstance(text, str) and text.strip():
                evidence.append(text.strip())
    return tuple(evidence)


def _assembled_memory_evidence(
    messages: Sequence[Mapping[str, Any]],
) -> str:
    return _safe_text("\n".join(_assembled_history_texts(messages)), 2400)
def _reference_chunks(prefix: str, value: str) -> tuple[ReviewReference, ...]:
    if not value:
        return ()
    chunks = tuple(value[index : index + 600] for index in range(0, len(value), 600))
    return tuple(
        ReviewReference(
            prefix if index == 0 else f"{prefix}.{index}",
            chunk,
        )
        for index, chunk in enumerate(chunks)
    )


def _safe_text(
    value: str,
    limit: int,
) -> str:
    cleaned = "".join(
        character
        for character in value
        if character in {
            "\n",
            "\r",
            "\t",
        }
        or ord(character) >= 32
    ).strip()
    return cleaned[:limit]


def _bounded_user_excerpt(value: str, limit: int) -> str:
    cleaned = _safe_text(value, max(len(value), 1))
    if len(cleaned) <= limit:
        return cleaned
    separator = "\n…\n"
    available = limit - len(separator)
    head = available // 3
    return f"{cleaned[:head]}{separator}{cleaned[-(available - head):]}"


def _complete_text(
    gateway: Gateway,
    messages: Sequence[
        Mapping[str, Any]
    ],
    timeout_seconds: float,
) -> str:
    async def invoke() -> str:
        request_id = f"quality-{uuid.uuid4().hex}"
        if bool(getattr(gateway, "stream_enabled", False)):
            async def collect_stream() -> str:
                chunks: list[str] = []
                async for delta in gateway.stream(
                    messages,
                    request_id=request_id,
                ):
                    if delta.text:
                        chunks.append(delta.text)
                return "".join(chunks)

            return await asyncio.wait_for(
                collect_stream(),
                timeout_seconds,
            )
        response = await asyncio.wait_for(
            gateway.complete(
                messages,
                request_id=request_id,
            ),
            timeout_seconds,
        )
        return response.text

    try:
        text = asyncio.run(invoke())
    except Exception:
        raise RuntimeError(
            "quality model unavailable"
        ) from None
    if (
        not isinstance(text, str)
        or not text.strip()
    ):
        raise RuntimeError(
            "quality model returned empty text"
        )
    return text


def _env_bool(
    name: str,
    default: bool,
) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _env_timeout(
    name: str,
    default: float,
) -> float:
    try:
        value = float(
            os.environ.get(
                name,
                default,
            )
        )
    except (TypeError, ValueError):
        value = default
    return max(
        0.1,
        min(
            120.0,
            value,
        ),
    )


__all__ = [
    "GatewayPersonaReviewer",
    "GatewayPersonaRewriter",
    "GatewayReviewTransport",
    "create_model_quality_ports",
]
