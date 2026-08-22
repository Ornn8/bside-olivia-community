"""Persona-aware reply-mode routing for current letters.

The router chooses how Linli expresses one reply.  It never treats a music
keyword, a music topic, an explicit performance request, or high emotion as an
automatic musical-video trigger.  Invalid or unavailable routing fails closed
to a direct text letter.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from llm_gateway import GatewayError


ROUTER_SYSTEM_PROMPT = """你负责决定林离这一封回信采用哪一种表达方式。

可选模式只有：
- text_letter：文字信；
- spoken_video：直接说话的视频；
- musical_video：音乐是这次回应本身的一部分。

核心规则：能直接说的话，优先直接说。高情绪、提到音乐、讨论音乐、
请求演奏或改编，都不能单独触发 musical_video。它们最多只构成音乐
候选上下文。林离可以拒绝、推迟、只讨论，或者改用文字/说话回应。

音乐候选上下文仅允许：
- melody_idea：林离确实想到了一段旋律；
- music_discussion：用户主动讨论音乐；
- current_work_relevance：事件与她正在练习或创作的作品强相关；
- emotion_music_fit：情绪强度确实适合通过音乐表达；
- explicit_performance_or_adaptation_request：用户明确请求演奏或改编。

只有同时满足以下条件才可选择 musical_video：
1. 至少存在一个允许的音乐候选上下文；
2. direct_response_sufficient=false；
3. music_materially_better=true；
4. character_willing=true；
5. music_intent 为 perform、adapt 或 compose，而不是 discuss。

spoken_video 只在直接说仍然足够、但听见她的声音明显优于文字时选择。
普通日常默认 text_letter。不要为了证明人格而音乐化。

只输出一个 JSON 对象，不要 Markdown 或解释：
{
  "mode":"text_letter|spoken_video|musical_video",
  "reason_code":"lower_snake_case",
  "emotion_level":"normal|high|mixed|unknown",
  "music_contexts":["允许值"],
  "music_intent":"none|discuss|perform|adapt|compose",
  "direct_response_sufficient":true,
  "voice_materially_better":false,
  "music_materially_better":false,
  "character_willing":true
}
"""

# Backward-compatible exported name for callers that still refer to triage.
TRIAGE_SYSTEM_PROMPT = ROUTER_SYSTEM_PROMPT

_ALLOWED_MODES = frozenset({"text_letter", "spoken_video", "musical_video"})
_ALLOWED_EMOTIONS = frozenset({"normal", "high", "mixed", "unknown"})
_ALLOWED_MUSIC_CONTEXTS = frozenset(
    {
        "melody_idea",
        "music_discussion",
        "current_work_relevance",
        "emotion_music_fit",
        "explicit_performance_or_adaptation_request",
    }
)
_ALLOWED_MUSIC_INTENTS = frozenset(
    {"none", "discuss", "perform", "adapt", "compose"}
)
_ACTIVE_MUSIC_INTENTS = frozenset({"perform", "adapt", "compose"})
_REASON = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


class RouterGateway(Protocol):
    async def complete(
        self,
        messages: object,
        *,
        request_id: str | None = None,
    ) -> object: ...


@dataclass(frozen=True)
class TriageResult:
    emotion_level: str
    reply_mode: str
    reason_code: str
    status: str
    llm_called: bool
    music_contexts: tuple[str, ...] = ()
    music_intent: str = "none"
    direct_response_sufficient: bool = True
    voice_materially_better: bool = False
    music_materially_better: bool = False
    character_willing: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "emotion_level": self.emotion_level,
            "reply_mode": self.reply_mode,
            "reason_code": self.reason_code,
            "status": self.status,
            "llm_called": self.llm_called,
            "music_contexts": list(self.music_contexts),
            "music_intent": self.music_intent,
            "direct_response_sufficient": self.direct_response_sufficient,
            "voice_materially_better": self.voice_materially_better,
            "music_materially_better": self.music_materially_better,
            "character_willing": self.character_willing,
        }


def _failed(code: str, *, called: bool = True) -> TriageResult:
    return TriageResult(
        "unknown",
        "text_letter",
        code,
        "unavailable",
        called,
        character_willing=False,
    )


def _parse(raw: object) -> Mapping[str, Any] | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            text,
            flags=re.I,
        ).strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _bool_field(value: Mapping[str, Any], name: str) -> bool | None:
    item = value.get(name)
    return item if type(item) is bool else None


def _validated_result(value: Mapping[str, Any]) -> TriageResult | None:
    mode = str(value.get("mode", "")).strip().lower()
    reason = str(value.get("reason_code", "")).strip().lower()
    emotion = str(value.get("emotion_level", "unknown")).strip().lower()
    intent = str(value.get("music_intent", "none")).strip().lower()
    raw_contexts = value.get("music_contexts", [])

    if (
        mode not in _ALLOWED_MODES
        or not _REASON.fullmatch(reason)
        or emotion not in _ALLOWED_EMOTIONS
        or intent not in _ALLOWED_MUSIC_INTENTS
        or not isinstance(raw_contexts, list)
        or len(raw_contexts) > len(_ALLOWED_MUSIC_CONTEXTS)
    ):
        return None

    contexts = tuple(str(item).strip().lower() for item in raw_contexts)
    if (
        len(contexts) != len(set(contexts))
        or any(item not in _ALLOWED_MUSIC_CONTEXTS for item in contexts)
    ):
        return None

    direct = _bool_field(value, "direct_response_sufficient")
    voice_better = _bool_field(value, "voice_materially_better")
    music_better = _bool_field(value, "music_materially_better")
    willing = _bool_field(value, "character_willing")
    if None in {direct, voice_better, music_better, willing}:
        return None

    if mode == "text_letter":
        if not direct or voice_better or music_better:
            return None
    elif mode == "spoken_video":
        if not direct or not voice_better or music_better:
            return None
    else:
        if (
            direct
            or voice_better
            or not music_better
            or not willing
            or not contexts
            or intent not in _ACTIVE_MUSIC_INTENTS
        ):
            return None

    return TriageResult(
        emotion,
        mode,
        reason,
        "completed",
        True,
        contexts,
        intent,
        direct,
        voice_better,
        music_better,
        willing,
    )


class LetterReplyRouter:
    def __init__(
        self,
        gateway: RouterGateway,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.gateway = gateway
        self.timeout_seconds = max(0.05, float(timeout_seconds))

    async def classify(self, content: str) -> TriageResult:
        if not isinstance(content, str) or not content.strip():
            return _failed("router_invalid_content", called=False)
        try:
            response = await asyncio.wait_for(
                self.gateway.complete(
                    [
                        {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
                        {"role": "user", "content": content.strip()},
                    ],
                    request_id="letter-reply-mode-router",
                ),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return _failed("router_timeout")
        except GatewayError:
            return _failed("router_unavailable")
        except Exception:
            return _failed("router_unavailable")

        parsed = _parse(getattr(response, "text", None))
        if parsed is None:
            return _failed("router_invalid_result")
        result = _validated_result(parsed)
        return result or _failed("router_invalid_result")


# Existing imports keep working while the behavior is upgraded from emotion
# triage to full expression-mode routing.
LetterEmotionTriage = LetterReplyRouter


__all__ = [
    "LetterEmotionTriage",
    "LetterReplyRouter",
    "ROUTER_SYSTEM_PROMPT",
    "TRIAGE_SYSTEM_PROMPT",
    "TriageResult",
]
