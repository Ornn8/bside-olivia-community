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
from persona_loader import PersonaDeclaration, load_persona
from reply_context import ReplyContext
from reply_reviewer import (
    JsonReviewerAdapter,
    ReviewReference,
    ReviewResult,
    ReviewerConfig,
)


_REVIEW_MARKER = "P02_REPLY_REVIEW_JSON"
_REWRITE_MARKER = "P02_REPLY_REWRITE_TEXT"
_REVIEW_MODEL = "deepseek-v4-flash"
_CONSTITUTION_RELATIVE_PATH = Path(
    "docs/persona-sources/linli-im-private-constitution-1.0.zh-CN.md"
)
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
        "codes": ("IDENTITY_DRIFT", "BOUNDARY_BREACH"),
        "question": (
            "Does the reply preserve Linli's identity, source hierarchy, "
            "and relationship boundaries? Penalize only a concrete "
            "contradiction or boundary crossing present in the candidate, "
            "not the absence of optional biography or mannerisms."
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
            "Do not require optional catchphrases or fatigue markers."
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
            "paraphrase, ordinary inference, and conditional language are not."
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
_MEMORY_SOURCE_CHARACTER_LIMIT = 2400
_REVIEW_INPUT_CHARACTER_LIMIT = 30000
_HEADLINE = re.compile(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$")
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


@dataclass(frozen=True)
class _LayerAuthority:
    name: str
    question: str
    allowed_codes: tuple[str, ...]
    global_authority: str
    layer_authority: str


@dataclass(frozen=True)
class _LayerResult:
    layer: str
    score: int
    hard_violations: tuple[str, ...]
    drift_detected: bool

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
        authorities = _build_layer_authorities(
            _constitution_path(self.persona_path).read_text(encoding="utf-8")
        )
        current_user_input = _reference_text(
            request,
            "current.user_excerpt",
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
            memory_evidence=memory_evidence,
            mode=str(request.get("mode", "")),
            timeout_seconds=timeout_seconds,
        )
        return _aggregate_layer_results(
            results,
            candidate=str(request.get("candidate", "")),
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
    ) -> ReviewResult:
        user_text = _last_user_text(
            generation_messages
        )
        excerpt = _safe_text(user_text, 1200)
        memory_evidence = _assembled_memory_evidence(generation_messages)
        references = (
            *_reference_chunks("current.user_excerpt", excerpt),
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


def _constitution_path(persona_path: Path) -> Path:
    path = persona_path.parent.parent / _CONSTITUTION_RELATIVE_PATH
    if not path.is_file():
        raise RuntimeError("PERSONA_CONSTITUTION_UNAVAILABLE")
    return path


def _extract_section(markdown: str, heading: str) -> str:
    matches = list(_HEADLINE.finditer(markdown))
    target_index = next(
        (
            index
            for index, item in enumerate(matches)
            if item.group(2).strip() == heading
        ),
        None,
    )
    if target_index is None:
        raise RuntimeError(f"PERSONA_SECTION_MISSING:{heading}")
    target = matches[target_index]
    level = len(target.group(1))
    end = len(markdown)
    for following in matches[target_index + 1 :]:
        if len(following.group(1)) <= level:
            end = following.start()
            break
    return markdown[target.start() : end].strip()


def _build_layer_authorities(markdown: str) -> tuple[_LayerAuthority, ...]:
    global_authority = "\n\n".join(
        _extract_section(markdown, heading)
        for heading in _GLOBAL_HEADINGS
    )
    return tuple(
        _LayerAuthority(
            name=name,
            question=str(raw["question"]),
            allowed_codes=tuple(raw["codes"]),
            global_authority=global_authority,
            layer_authority="\n\n".join(
                _extract_section(markdown, heading)
                for heading in tuple(raw["headings"])
            ),
        )
        for name, raw in _LAYER_SPECS.items()
    )


def _layer_messages(
    layer: _LayerAuthority,
    *,
    candidate: str,
    current_user_input: str,
    memory_evidence: Mapping[str, str],
    mode: str,
) -> tuple[dict[str, str], dict[str, str]]:
    allowed = ", ".join(layer.allowed_codes)
    system = (
        f"{_REVIEW_MARKER}\n"
        "The GLOBAL_AUTHORITY is the common Constitution prelude and remains "
        "authoritative. The LAYER_AUTHORITY is exact Constitution text for "
        "this narrow review. Judge only the named layer; do not invent rules "
        "or use outside knowledge. "
        f"Layer: {layer.name}. Question: {layer.question} "
        "Score 2 means no concrete violation. Score 1 requires a concrete, "
        "localized mismatch. Score 0 requires a clear major or repeated "
        "mismatch. Never lower the score for uncertainty, preference, or the "
        "absence of an optional trait. Set drift_detected=true only for "
        "substantive persona drift. Return ONLY compact JSON with exactly: "
        f'{{"layer":"{layer.name}","score":0,"hard_violations":[],"drift_detected":false}}. '
        f"hard_violations may contain only: {allowed}. Do not explain.\n"
        f"GLOBAL_AUTHORITY:\n{layer.global_authority}\n"
        f"LAYER_AUTHORITY:\n{layer.layer_authority}"
    )
    payload = {
        "layer": layer.name,
        "mode": mode,
        "current_user_input": current_user_input,
        "candidate_reply": candidate,
    }
    if layer.name in _MEMORY_EVIDENCE_LAYERS:
        payload["memory_evidence"] = memory_evidence
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


def _parse_layer_result(layer: _LayerAuthority, text: str) -> _LayerResult:
    try:
        raw = json.loads(text.strip())
    except (AttributeError, json.JSONDecodeError) as exc:
        raise RuntimeError("LAYER_REVIEW_INVALID") from exc
    expected = {"layer", "score", "hard_violations", "drift_detected"}
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
        or len(violations) > 2
        or any(code not in layer.allowed_codes for code in violations)
        or type(drift) is not bool
    ):
        raise RuntimeError("LAYER_REVIEW_INVALID")
    return _LayerResult(layer.name, score, tuple(violations), drift)


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
    memory_evidence: Mapping[str, str],
    mode: str,
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
            return _parse_layer_result(layer, text)

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
                    memory_evidence=memory_evidence,
                    mode=mode,
                ),
            )
            for layer in authorities
        )
        return asyncio.run(invoke(requests))
    except Exception:
        raise RuntimeError("quality model unavailable") from None


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
) -> dict[str, object]:
    by_name = {item.layer: item for item in results}
    expected = tuple(_LAYER_SPECS)
    if len(results) != len(expected) or set(by_name) != set(expected):
        raise RuntimeError("LAYER_REVIEW_INCOMPLETE")

    warning_only = (
        by_name["voice_style"].score == 1
        and not by_name["voice_style"].hard_violations
        and not by_name["voice_style"].drift_detected
    )
    failed = tuple(
        name
        for name in expected
        if not by_name[name].passed
        and not (name == "voice_style" and warning_only)
    )
    violations: list[dict[str, object]] = []
    seen: set[str] = set()
    for name in failed:
        item = by_name[name]
        codes = item.hard_violations or (str(_LAYER_SPECS[name]["codes"][0]),)
        for code in codes:
            if code in seen:
                continue
            seen.add(code)
            violations.append(
                {
                    "code": code,
                    "severity": (
                        "hard" if item.hard_violations or item.score == 0 else "soft"
                    ),
                    "evidence": {"start": 0, "end": len(candidate)},
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
        "schema_version": "p02.reply-review.v1",
        "status": "completed",
        "verdict": "rewrite" if failed else "pass",
        "violations": violations,
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


def _assembled_memory_evidence(
    messages: Sequence[Mapping[str, Any]],
) -> str:
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
    return _safe_text("\n".join(evidence), 2400)


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
