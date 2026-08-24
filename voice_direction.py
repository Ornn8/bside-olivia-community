"""LLM-directed, non-spoken performance controls for a frozen reply."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence


class VoiceDirectionError(RuntimeError):
    """The voice-director response cannot safely drive the frozen reply."""


@dataclass(frozen=True)
class VoiceToolCall:
    name: str
    arguments: Mapping[str, Any]


class VoiceToolGateway(Protocol):
    async def complete_with_tools(
        self,
        *,
        messages: Sequence[Mapping[str, str]],
        tools: Sequence[Mapping[str, object]],
        tool_choice: str,
        request_id: str | None = None,
    ) -> Sequence[VoiceToolCall]: ...


@dataclass(frozen=True)
class VoiceSpeechUnit:
    text: str
    cue_index: int
    speed: float
    pause_after_seconds: float
    gain_db: float


@dataclass(frozen=True)
class VoicePerformanceSegment:
    text: str
    sentence_start: int
    sentence_end: int
    emotion: str
    intensity: float
    speed: float
    pause_after_seconds: float
    gain_db: float

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "sentence_start": self.sentence_start,
            "sentence_end": self.sentence_end,
            "emotion": self.emotion,
            "intensity": self.intensity,
            "speed": self.speed,
            "pause_after_seconds": self.pause_after_seconds,
            "gain_db": self.gain_db,
        }


@dataclass(frozen=True)
class VoicePerformancePlan:
    reply_text: str
    segments: tuple[VoicePerformanceSegment, ...]
    overall_emotion: str = "natural, warm conversation"
    global_speed: float = 1.06
    energy: float = 0.55
    breath_before_sentences: tuple[int, ...] = ()
    emphasize_sentences: tuple[int, ...] = ()
    source: str = "llm_tool_call"
    control_channel: str = "non_spoken"
    duration_target_seconds: tuple[float, float] = (40.0, 50.0)

    @property
    def spoken_text(self) -> str:
        return "".join(segment.text for segment in self.segments)

    @property
    def cues(self) -> tuple[VoicePerformanceSegment, ...]:
        """Compatibility with the maintained delivery renderer."""

        return self.segments

    @property
    def render_text(self) -> str:
        """Return one marked-up utterance; removing supported tags yields the frozen text."""

        sentences = _sentences(self.reply_text)
        rendered: list[str] = []
        for sentence_id, sentence in enumerate(sentences, 1):
            prefix = "[breath]" if sentence_id in self.breath_before_sentences else ""
            if sentence_id in self.emphasize_sentences:
                rendered.append(prefix + "<strong>" + sentence + "</strong>")
            else:
                rendered.append(prefix + sentence)
        return "".join(rendered)

    def speech_units(self) -> tuple[VoiceSpeechUnit, ...]:
        return (
            VoiceSpeechUnit(
                text=self.render_text,
                cue_index=0,
                speed=self.global_speed,
                pause_after_seconds=0.0,
                gain_db=max(-0.75, min(0.75, (self.energy - 0.5) * 1.5)),
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "reply_text": self.reply_text,
            "render_text": self.render_text,
            "segments": [segment.to_dict() for segment in self.segments],
            "overall_emotion": self.overall_emotion,
            "global_speed": self.global_speed,
            "energy": self.energy,
            "breath_before_sentences": list(self.breath_before_sentences),
            "emphasize_sentences": list(self.emphasize_sentences),
            "source": self.source,
            "control_channel": self.control_channel,
            "duration_target_seconds": list(self.duration_target_seconds),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "VoicePerformancePlan":
        reply_text = value.get("reply_text")
        raw_segments = value.get("segments")
        if not isinstance(reply_text, str) or not isinstance(raw_segments, list):
            raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
        segments: list[VoicePerformanceSegment] = []
        try:
            for raw in raw_segments:
                if not isinstance(raw, Mapping):
                    raise ValueError
                segments.append(
                    VoicePerformanceSegment(
                        text=str(raw["text"]),
                        sentence_start=int(raw["sentence_start"]),
                        sentence_end=int(raw["sentence_end"]),
                        emotion=str(raw["emotion"]),
                        intensity=float(raw["intensity"]),
                        speed=float(raw["speed"]),
                        pause_after_seconds=float(raw["pause_after_seconds"]),
                        gain_db=float(raw["gain_db"]),
                    )
                )
        except (KeyError, TypeError, ValueError):
            raise VoiceDirectionError("VOICE_DIRECTION_INVALID") from None
        try:
            overall_emotion = str(value.get("overall_emotion", "natural, warm conversation"))
            global_speed = float(value.get("global_speed", 1.06))
            energy = float(value.get("energy", 0.55))
            breath_before = tuple(int(item) for item in value.get("breath_before_sentences", []))
            emphasize = tuple(int(item) for item in value.get("emphasize_sentences", []))
        except (TypeError, ValueError):
            raise VoiceDirectionError("VOICE_DIRECTION_INVALID") from None
        plan = cls(
            reply_text=reply_text,
            segments=tuple(segments),
            overall_emotion=overall_emotion,
            global_speed=global_speed,
            energy=energy,
            breath_before_sentences=breath_before,
            emphasize_sentences=emphasize,
        )
        if not segments or plan.spoken_text != reply_text:
            raise VoiceDirectionError("VOICE_DIRECTION_TEXT_MISMATCH")
        return plan


_TOOL = {
    "type": "function",
    "function": {
        "name": "apply_voice_performance",
        "description": (
            "Direct one continuous Chinese utterance without rewriting it. "
            "Choose one global performance and only supported non-spoken sentence marks."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "overall_emotion",
                "global_speed",
                "energy",
                "breath_before_sentences",
                "emphasize_sentences",
                "segments",
            ],
            "properties": {
                "overall_emotion": {
                    "type": "string",
                    "description": "One concise whole-utterance acting intention, not spoken text.",
                },
                "global_speed": {
                    "type": "number",
                    "minimum": 1.02,
                    "maximum": 1.08,
                    "description": "Natural conversational pace; default near 1.06, never lethargic.",
                },
                "energy": {
                    "type": "number",
                    "minimum": 0.35,
                    "maximum": 0.8,
                },
                "breath_before_sentences": {
                    "type": "array",
                    "maxItems": 2,
                    "uniqueItems": True,
                    "items": {"type": "integer", "minimum": 2},
                },
                "emphasize_sentences": {
                    "type": "array",
                    "maxItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "integer", "minimum": 1},
                },
                "segments": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "sentence_start",
                            "sentence_end",
                            "emotion",
                            "intensity",
                        ],
                        "properties": {
                            "sentence_start": {"type": "integer", "minimum": 1},
                            "sentence_end": {"type": "integer", "minimum": 1},
                            "emotion": {
                                "type": "string",
                                "description": "Short acting intention, not spoken text.",
                            },
                            "intensity": {
                                "type": "number",
                                "minimum": 0.0,
                                "maximum": 1.0,
                            },
                        },
                    },
                }
            },
        },
    },
}


def _sentences(text: str) -> tuple[str, ...]:
    if not isinstance(text, str) or not text.strip():
        raise VoiceDirectionError("VOICE_DIRECTION_EMPTY_REPLY")
    parts: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        if text[index] not in "。！？!?；;":
            index += 1
            continue
        end = index + 1
        while end < len(text) and text[end] in "”’\"'」』】）)":
            end += 1
        while end < len(text) and text[end].isspace():
            end += 1
        parts.append(text[start:end])
        start = end
        index = end
    if start < len(text):
        parts.append(text[start:])
    return tuple(part for part in parts if part)


def _number(value: object, *, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise VoiceDirectionError(f"VOICE_DIRECTION_{name.upper()}_INVALID")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise VoiceDirectionError(f"VOICE_DIRECTION_{name.upper()}_INVALID") from None
    if not minimum <= result <= maximum:
        raise VoiceDirectionError(f"VOICE_DIRECTION_{name.upper()}_INVALID")
    return result


async def direct_voice_performance(
    reply_text: str,
    gateway: VoiceToolGateway,
    *,
    request_id: str | None = None,
) -> VoicePerformancePlan:
    """Ask a second LLM call to direct, never rewrite, the frozen reply."""

    sentences = _sentences(reply_text)
    indexed = "\n".join(f"S{index}: {text}" for index, text in enumerate(sentences, 1))
    messages = [
        {
            "role": "system",
            "content": (
                "你是 Olivia 普通视频回信的声音导演。正文已定稿，禁止改字、补字或把控制提示当台词。"
                "必须调用 apply_voice_performance。整篇语音只生成一次，不按段分别推理。"
                "选择一个贯穿全文的自然表演方向和全局语速；正常对话默认约 1.06，避免拖沓或一字一顿。"
                "局部变化只能使用少量句前呼吸和一句重音标记，二者都是模型官方支持的非发声控制。"
                "segments 只描述渐进的情绪理解，不提供分段语速、分段停顿或分段响度。"
            ),
        },
        {
            "role": "user",
            "content": (
                "以下冻结正文仅供表演理解。请覆盖每个句子一次且仅一次，分段必须连续；不要复述正文。"
                "\n\n【完整冻结正文】\n"
                + reply_text
                + "\n\n【句子编号】\n"
                + indexed
            ),
        },
    ]
    try:
        calls = await gateway.complete_with_tools(
            messages=messages,
            tools=[_TOOL],
            tool_choice="required",
            request_id=request_id,
        )
    except TypeError as exc:
        # Small injected fakes may predate the optional request_id keyword.
        if request_id is not None:
            raise VoiceDirectionError("VOICE_DIRECTION_GATEWAY_INVALID") from exc
        calls = await gateway.complete_with_tools(
            messages=messages,
            tools=[_TOOL],
            tool_choice="required",
        )
    if len(calls) != 1:
        raise VoiceDirectionError("voice director must return exactly one tool call")
    call = calls[0]
    if call.name != "apply_voice_performance":
        raise VoiceDirectionError("VOICE_DIRECTION_TOOL_INVALID")
    raw_segments = call.arguments.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments or len(raw_segments) > 5:
        raise VoiceDirectionError("VOICE_DIRECTION_SEGMENTS_INVALID")

    overall_emotion = str(call.arguments.get("overall_emotion", "natural, warm conversation")).strip()
    if (
        not overall_emotion
        or len(overall_emotion) > 80
        or any(token in overall_emotion for token in ("<|", "|>", "[", "]", "<", ">"))
    ):
        raise VoiceDirectionError("VOICE_DIRECTION_EMOTION_INVALID")
    global_speed = _number(
        call.arguments.get("global_speed", 1.06),
        name="global_speed",
        minimum=1.02,
        maximum=1.08,
    )
    energy = _number(
        call.arguments.get("energy", 0.55),
        name="energy",
        minimum=0.35,
        maximum=0.8,
    )
    def safe_sentence_ids(value: object, *, minimum: int, maximum_items: int) -> tuple[int, ...]:
        if not isinstance(value, list):
            return ()
        accepted: list[int] = []
        for item in value:
            try:
                sentence_id = int(item)
            except (TypeError, ValueError):
                continue
            if minimum <= sentence_id <= len(sentences) and sentence_id not in accepted:
                accepted.append(sentence_id)
            if len(accepted) == maximum_items:
                break
        return tuple(sorted(accepted))

    # Optional acoustic marks fail soft: invalid marks are omitted, never moved.
    breath_before = safe_sentence_ids(
        call.arguments.get("breath_before_sentences", []), minimum=2, maximum_items=2
    )
    emphasize = safe_sentence_ids(
        call.arguments.get("emphasize_sentences", []), minimum=1, maximum_items=1
    )

    expected_start = 1
    segments: list[VoicePerformanceSegment] = []
    for raw in raw_segments:
        if not isinstance(raw, Mapping):
            raise VoiceDirectionError("VOICE_DIRECTION_SEGMENTS_INVALID")
        try:
            sentence_start = int(raw["sentence_start"])
            sentence_end = int(raw["sentence_end"])
            emotion = str(raw["emotion"]).strip()
        except (KeyError, TypeError, ValueError):
            raise VoiceDirectionError("VOICE_DIRECTION_SEGMENTS_INVALID") from None
        if sentence_start != expected_start or sentence_end < sentence_start:
            raise VoiceDirectionError("segments must cover every sentence exactly once")
        if sentence_end > len(sentences) or not emotion or len(emotion) > 40:
            raise VoiceDirectionError("VOICE_DIRECTION_SEGMENTS_INVALID")
        segment_text = "".join(sentences[sentence_start - 1 : sentence_end])
        segments.append(
            VoicePerformanceSegment(
                text=segment_text,
                sentence_start=sentence_start,
                sentence_end=sentence_end,
                emotion=emotion,
                intensity=_number(raw.get("intensity"), name="intensity", minimum=0, maximum=1),
                speed=global_speed,
                pause_after_seconds=0.0,
                gain_db=max(-0.75, min(0.75, (energy - 0.5) * 1.5)),
            )
        )
        expected_start = sentence_end + 1
    if expected_start != len(sentences) + 1:
        raise VoiceDirectionError("segments must cover every sentence exactly once")
    plan = VoicePerformancePlan(
        reply_text=reply_text,
        segments=tuple(segments),
        overall_emotion=overall_emotion,
        global_speed=global_speed,
        energy=energy,
        breath_before_sentences=breath_before,
        emphasize_sentences=emphasize,
    )
    if plan.spoken_text != reply_text:
        raise VoiceDirectionError("VOICE_DIRECTION_TEXT_MISMATCH")
    return plan
