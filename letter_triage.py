"""Emotion gate for expensive video replies.

The user's letter is classified before media work.  Any provider or parse
failure fails closed to a text reply; this module never writes the letter
body or provider response to disk.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from llm_gateway import Gateway, GatewayError

TRIAGE_SYSTEM_PROMPT = (
    "你是信件情绪分流器，只判断用户来信本身的情绪波动。"
    "长期压抑的痛苦、背叛创伤、不安全感、被替代感、孤独、重大失落、"
    "强烈羞耻、恐惧或悲伤判定为 high；日常寒暄、普通聊天和轻微情绪判定为 normal。"
    "视频、唱歌或音乐请求不是 high 证据。只输出 JSON："
    '{"emotion_level":"high"或"normal","reason_code":"short_code"}'
)
_REASON = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


@dataclass(frozen=True)
class TriageResult:
    emotion_level: str
    reply_mode: str
    reason_code: str
    status: str
    llm_called: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "emotion_level": self.emotion_level,
            "reply_mode": self.reply_mode,
            "reason_code": self.reason_code,
            "status": self.status,
            "llm_called": self.llm_called,
        }


def _failed(code: str, *, called: bool = True) -> TriageResult:
    return TriageResult("unknown", "text", code, "unavailable", called)


def _parse(raw: object) -> Mapping[str, Any] | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


class LetterEmotionTriage:
    def __init__(self, gateway: Gateway, *, timeout_seconds: float = 10.0) -> None:
        self.gateway = gateway
        self.timeout_seconds = max(0.05, float(timeout_seconds))

    async def classify(self, content: str) -> TriageResult:
        if not isinstance(content, str) or not content.strip():
            return _failed("TRIAGE_INVALID_CONTENT", called=False)
        try:
            response = await asyncio.wait_for(
                self.gateway.complete(
                    [
                        {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
                        {"role": "user", "content": content.strip()},
                    ],
                    request_id="letter-emotion-triage",
                ),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return _failed("TRIAGE_LLM_TIMEOUT")
        except GatewayError:
            return _failed("TRIAGE_LLM_UNAVAILABLE")
        except Exception:
            return _failed("TRIAGE_LLM_UNAVAILABLE")
        parsed = _parse(getattr(response, "text", None))
        level = str(parsed.get("emotion_level", "")).strip().lower() if parsed else ""
        if level not in {"high", "normal"}:
            return _failed("TRIAGE_INVALID_RESULT")
        reason = str(parsed.get("reason_code", "")).strip().lower().replace(" ", "_")
        if not _REASON.fullmatch(reason):
            reason = "emotion_high" if level == "high" else "emotion_normal"
        return TriageResult(level, "video" if level == "high" else "text", reason, "completed", True)


__all__ = ["LetterEmotionTriage", "TriageResult", "TRIAGE_SYSTEM_PROMPT"]
