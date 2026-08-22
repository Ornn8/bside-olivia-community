"""Configured-model reviewer and one-shot rewrite adapters for reply quality."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from llm_gateway import Gateway
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
        payload = {
            **request,
            "persona": _persona_review_profile(
                self.persona_path,
                str(request.get("mode", "")),
            ),
        }
        messages = (
            {
                "role": "system",
                "content": (
                    f"{_REVIEW_MARKER}\n"
                    "You are a strict reply evaluator. Return exactly one JSON object, "
                    "without Markdown or commentary. The object must use schema_version "
                    "p02.reply-review.v1, status completed, verdict pass/rewrite/block, "
                    "violations with hard/soft severity and zero-based Unicode start/end "
                    "spans into candidate, and four integer scores from 0 to 100. Check "
                    "Linli identity and autonomy, generic-assistant or counselling drift, "
                    "invented shared history or facts, forced music imagery, relationship "
                    "pressure, internal-state leakage, and current channel constraints."
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
        text = _complete_text(
            self.gateway,
            messages,
            timeout_seconds,
        )
        parsed = json.loads(text.strip())
        if not isinstance(parsed, Mapping):
            raise ValueError(
                "review response must be an object"
            )
        return dict(parsed)


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
                model="configured_model",
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
        excerpt = _safe_excerpt(user_text)
        references = (
            (
                ReviewReference(
                    "current.user_excerpt",
                    excerpt,
                ),
            )
            if excerpt
            else ()
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
                3000,
            ),
            "candidate": candidate,
            "violation_codes": list(
                violation_codes
            ),
        }
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

    configured_timeout = float(
        getattr(
            config,
            "timeout_seconds",
            30.0,
        )
    )
    timeout = _env_timeout(
        "OLIVIA_REPLY_REVIEW_TIMEOUT_SECONDS",
        min(configured_timeout, 12.0),
    )
    reviewer = GatewayPersonaReviewer(
        gateway,
        persona_path,
        timeout,
    )
    rewriter = (
        GatewayPersonaRewriter(
            gateway,
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


def _safe_excerpt(value: str) -> str:
    return _safe_text(value, 600)


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
        response = await asyncio.wait_for(
            gateway.complete(
                messages,
                request_id=(
                    f"quality-{uuid.uuid4().hex}"
                ),
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
