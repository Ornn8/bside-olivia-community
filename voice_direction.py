"""Fail-closed LLM direction for one frozen reply performance."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import re
from typing import Any, Mapping, Protocol, Sequence


class VoiceDirectionError(RuntimeError):
    """A director response cannot safely control the frozen reply."""


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
    """A local compatibility view, never a provider-authored control surface."""

    text: str
    sentence_start: int
    sentence_end: int
    emotion: str
    intensity: float
    speed: float
    pause_after_seconds: float
    gain_db: float


_SOURCE = "llm_tool_call"
_CONTROL_CHANNEL = "non_spoken"
_PROFILE = "cosyvoice3_base_a_v1"
_MUSIC_PROFILE = "legacy_music_global_direction_v1"
_DURATION_TARGET = (40.0, 50.0)
_TOOL_FIELDS = frozenset({"short_instruction"})
_MUSIC_TOOL_FIELDS = frozenset(
    {"overall_emotion", "global_speed", "energy", "breath_before_sentences", "emphasize_sentences"}
)
_PERSISTED_FIELDS = frozenset({
    "reply_text",
    "overall_emotion",
    "global_speed",
    "energy",
    "breath_before_sentences",
    "emphasize_sentences",
    "short_instruction",
    "source",
    "control_channel",
    "profile",
    "duration_target_seconds",
})
_LEGACY_MUSIC_PERSISTED_FIELDS = _MUSIC_TOOL_FIELDS | {
    "reply_text",
    "source",
    "control_channel",
    "duration_target_seconds",
}
_ALLOWED_INSTRUCTION_RE = re.compile(r"[\u3400-\u9fff，。！？、；：…—]+")


@dataclass(frozen=True)
class VoicePerformancePlan:
    """One whole-reply direction; text identity is always derived locally."""

    reply_text: str
    overall_emotion: str
    global_speed: float
    energy: float
    breath_before_sentences: tuple[int, ...]
    emphasize_sentences: tuple[int, ...]
    short_instruction: str = "声音柔软自然地承接，再缓缓托起给到力量"
    source: str = _SOURCE
    control_channel: str = _CONTROL_CHANNEL
    profile: str = _PROFILE
    duration_target_seconds: tuple[float, float] = _DURATION_TARGET

    def __post_init__(self) -> None:
        _validate_plan(self)

    @property
    def spoken_text(self) -> str:
        return self.reply_text

    @property
    def render_text(self) -> str:
        """Compatibility text channel; sparse marks remain structured controls."""

        return self.reply_text

    @property
    def cues(self) -> tuple[VoicePerformanceSegment, ...]:
        """Expose exactly one locally-derived cue for maintained renderers."""

        sentence_count = len(_sentences(self.reply_text))
        return (
            VoicePerformanceSegment(
                text=self.reply_text,
                sentence_start=1,
                sentence_end=sentence_count,
                emotion=self.overall_emotion,
                intensity=self.energy,
                speed=self.global_speed,
                pause_after_seconds=0.0,
                gain_db=_gain_db(self.energy),
            ),
        )

    def speech_units(self) -> tuple[VoiceSpeechUnit, ...]:
        return (
            VoiceSpeechUnit(
                text=self.render_text,
                cue_index=0,
                speed=self.global_speed,
                pause_after_seconds=0.0,
                gain_db=_gain_db(self.energy),
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "reply_text": self.reply_text,
            "overall_emotion": self.overall_emotion,
            "global_speed": self.global_speed,
            "energy": self.energy,
            "breath_before_sentences": list(self.breath_before_sentences),
            "emphasize_sentences": list(self.emphasize_sentences),
            "short_instruction": self.short_instruction,
            "source": self.source,
            "control_channel": self.control_channel,
            "profile": self.profile,
            "duration_target_seconds": list(self.duration_target_seconds),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "VoicePerformancePlan":
        return _persisted_plan(cls, value, profile=_PROFILE, minimum_speed=1.0)

    @classmethod
    def from_music_dict(cls, value: Mapping[str, object]) -> "VoicePerformancePlan":
        if set(value) == _LEGACY_MUSIC_PERSISTED_FIELDS:
            value = {**value, "short_instruction": "", "profile": _MUSIC_PROFILE}
        return _persisted_plan(
            cls, value, profile=_MUSIC_PROFILE, minimum_speed=1.02
        )


def _persisted_plan(
    cls: type[VoicePerformancePlan],
    value: Mapping[str, object],
    *,
    profile: str,
    minimum_speed: float,
) -> VoicePerformancePlan:
    if set(value) != _PERSISTED_FIELDS:
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
    reply_text, emotion = value["reply_text"], value["overall_emotion"]
    source, channel = value["source"], value["control_channel"]
    if not all(isinstance(item, str) for item in (reply_text, emotion, source, channel)):
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
    if profile != _PROFILE and value["short_instruction"] != "":
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
    sentence_count = len(_sentences(reply_text))
    plan = cls(
        reply_text=reply_text,
        overall_emotion=emotion,
        global_speed=_number(value["global_speed"], minimum=minimum_speed, maximum=1.08),
        energy=_number(value["energy"], minimum=0.35, maximum=0.8),
        breath_before_sentences=_sentence_marks(
            value["breath_before_sentences"], sentence_count=sentence_count, minimum=2, maximum_items=2
        ),
        emphasize_sentences=_sentence_marks(
            value["emphasize_sentences"], sentence_count=sentence_count, minimum=1, maximum_items=1
        ),
        short_instruction=(
            validate_short_instruction(value["short_instruction"])
            if profile == _PROFILE
            else ""
        ),
        source=source,
        control_channel=channel,
        profile=str(value["profile"]),
        duration_target_seconds=_duration_target(value["duration_target_seconds"]),
    )
    if plan.profile != profile:
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
    return plan


_TOOL = {
    "type": "function",
    "function": {
        "name": "apply_voice_performance",
        "description": "Choose one short, non-spoken direction for a frozen utterance.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(_TOOL_FIELDS),
            "properties": {
                "short_instruction": {
                    "type": "string",
                    "minLength": 12,
                    "maxLength": 24,
                    "pattern": "^[\\u3400-\\u9fff，。！？、；：…—]+$",
                    "description": "一条正向具体的中文表演指令，不复述正文。",
                },
            },
        },
    },
}

_MUSIC_TOOL = {
    "type": "function",
    "function": {
        "name": "apply_voice_performance",
        "description": "Direct one frozen utterance with global, non-spoken controls only.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(_MUSIC_TOOL_FIELDS),
            "properties": {
                "overall_emotion": {"type": "string", "description": "One concise whole-utterance acting intention."},
                "global_speed": {"type": "number", "minimum": 1.02, "maximum": 1.08},
                "energy": {"type": "number", "minimum": 0.35, "maximum": 0.8},
                "breath_before_sentences": {
                    "type": "array", "maxItems": 2, "uniqueItems": True,
                    "items": {"type": "integer", "minimum": 2},
                },
                "emphasize_sentences": {
                    "type": "array", "maxItems": 1, "uniqueItems": True,
                    "items": {"type": "integer", "minimum": 1},
                },
            },
        },
    },
}


def _sentences(text: str) -> tuple[str, ...]:
    if not isinstance(text, str) or not text.strip():
        raise VoiceDirectionError("VOICE_DIRECTION_EMPTY_REPLY")
    parts: list[str] = []
    start = index = 0
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
        start = index = end
    if start < len(text):
        parts.append(text[start:])
    return tuple(part for part in parts if part)


def _number(value: object, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID") from None
    if not isfinite(result) or not minimum <= result <= maximum:
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
    return result


def _sentence_marks(value: object, *, sentence_count: int, minimum: int, maximum_items: int) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
    if any(not isinstance(item, int) or isinstance(item, bool) for item in value):
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
    if len(set(value)) != len(value) or any(item < minimum or item > sentence_count for item in value):
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
    return tuple(sorted(value))


def _duration_target(value: object) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
    parsed = tuple(_number(item, minimum=0.0, maximum=3600.0) for item in value)
    if parsed != _DURATION_TARGET:
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
    return parsed


def validate_short_instruction(value: object) -> str:
    if not isinstance(value, str):
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
    instruction = value.strip().rstrip("。")
    han_count = len(re.findall(r"[\u3400-\u9fff]", instruction))
    forbidden = (
        "不要",
        "禁止",
        "避免",
        "不能",
        "不可",
        "别",
        "勿",
        "不",
        "无",
        "朗读",
        "提示词",
    )
    if (
        not 12 <= han_count <= 24
        or _ALLOWED_INSTRUCTION_RE.fullmatch(instruction) is None
        or any(token in instruction for token in forbidden)
    ):
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
    return instruction


def _validate_plan(plan: VoicePerformancePlan) -> None:
    _sentences(plan.reply_text)
    if (
        not plan.overall_emotion.strip()
        or len(plan.overall_emotion) > 80
        or any(token in plan.overall_emotion for token in ("<|", "|>", "[", "]", "<", ">"))
        or plan.source != _SOURCE
        or plan.control_channel != _CONTROL_CHANNEL
        or plan.profile not in {_PROFILE, _MUSIC_PROFILE}
    ):
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
    if plan.profile == _PROFILE:
        validate_short_instruction(plan.short_instruction)
        _number(plan.global_speed, minimum=1.0, maximum=1.08)
    else:
        if plan.short_instruction:
            raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
        _number(plan.global_speed, minimum=1.02, maximum=1.08)
    _number(plan.energy, minimum=0.35, maximum=0.8)
    sentence_count = len(_sentences(plan.reply_text))
    if _sentence_marks(list(plan.breath_before_sentences), sentence_count=sentence_count, minimum=2, maximum_items=2) != plan.breath_before_sentences:
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
    if _sentence_marks(list(plan.emphasize_sentences), sentence_count=sentence_count, minimum=1, maximum_items=1) != plan.emphasize_sentences:
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
    if plan.duration_target_seconds != _DURATION_TARGET:
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")


def _gain_db(energy: float) -> float:
    return max(-0.75, min(0.75, (energy - 0.5) * 1.5))


async def _tool_arguments(
    gateway: VoiceToolGateway,
    messages: list[dict[str, str]],
    tool: Mapping[str, object],
    fields: frozenset[str],
    request_id: str | None,
) -> Mapping[str, Any]:
    kwargs = {"messages": messages, "tools": [tool], "tool_choice": "required"}
    try:
        calls = await gateway.complete_with_tools(**kwargs, request_id=request_id)
    except TypeError as exc:
        if request_id is not None:
            raise VoiceDirectionError("VOICE_DIRECTION_GATEWAY_INVALID") from exc
        calls = await gateway.complete_with_tools(**kwargs)
    if len(calls) != 1 or calls[0].name != "apply_voice_performance":
        raise VoiceDirectionError("VOICE_DIRECTION_TOOL_INVALID")
    arguments = calls[0].arguments
    if not isinstance(arguments, Mapping) or set(arguments) != fields:
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
    return arguments


async def direct_voice_performance(
    reply_text: str,
    gateway: VoiceToolGateway,
    *,
    letter_content: str | None = None,
    request_id: str | None = None,
) -> VoicePerformancePlan:
    """Ask a second LLM call for global controls without changing frozen text."""

    sentences = _sentences(reply_text)
    indexed = "\n".join(f"S{index}: {text}" for index, text in enumerate(sentences, 1))
    messages = [
        {
            "role": "system",
            "content": (
                "你是 Olivia 普通视频回信的声音导演。正文已定稿，禁止改字、补字或复述台词。"
                "必须调用 apply_voice_performance，并结合原始来信与完整回信判断声音情绪。"
                "short_instruction 只写一条正向、具体、12至24个汉字的表演指令，最多一次平滑转折；"
                "语速固定为自然对话 1.0，停顿只由正文标点决定。"
            ),
        },
        {
            "role": "user",
            "content": (
                "以下内容仅供表演理解，不要复述。\n\n【原始来信】\n"
                + str(letter_content or "未提供；仅依据回信判断")
                + "\n\n【完整冻结回信】\n"
                + reply_text
                + "\n\n【句子编号】\n"
                + indexed
            ),
        },
    ]
    arguments = await _tool_arguments(
        gateway, messages, _TOOL, _TOOL_FIELDS, request_id
    )
    short_instruction = validate_short_instruction(arguments["short_instruction"])
    return VoicePerformancePlan(
        reply_text=reply_text,
        overall_emotion=short_instruction,
        global_speed=1.0,
        energy=0.55,
        breath_before_sentences=(),
        emphasize_sentences=(),
        short_instruction=short_instruction,
    )


async def direct_music_voice_performance(
    reply_text: str,
    gateway: VoiceToolGateway,
    *,
    request_id: str | None = None,
) -> VoicePerformancePlan:
    """Preserve the pre-A global director contract for the musical prelude."""

    sentences = _sentences(reply_text)
    indexed = "\n".join(f"S{index}: {text}" for index, text in enumerate(sentences, 1))
    messages = [
        {
            "role": "system",
            "content": (
                "你是 Olivia 普通视频回信的声音导演。正文已定稿，禁止改字、补字或复述台词。"
                "必须调用 apply_voice_performance。只提供整篇 overall_emotion、global_speed、energy，"
                "以及极少的句前呼吸和一句重音标记；不得输出逐段情绪、强度、速度、停顿或响度。"
            ),
        },
        {
            "role": "user",
            "content": "以下冻结正文仅供表演理解。不要复述正文。\n\n【完整冻结正文】\n" + reply_text + "\n\n【句子编号】\n" + indexed,
        },
    ]
    arguments = await _tool_arguments(
        gateway, messages, _MUSIC_TOOL, _MUSIC_TOOL_FIELDS, request_id
    )
    overall_emotion = arguments["overall_emotion"]
    if not isinstance(overall_emotion, str):
        raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
    return VoicePerformancePlan(
        reply_text=reply_text,
        overall_emotion=overall_emotion,
        global_speed=_number(arguments["global_speed"], minimum=1.02, maximum=1.08),
        energy=_number(arguments["energy"], minimum=0.35, maximum=0.8),
        breath_before_sentences=_sentence_marks(
            arguments["breath_before_sentences"],
            sentence_count=len(sentences),
            minimum=2,
            maximum_items=2,
        ),
        emphasize_sentences=_sentence_marks(
            arguments["emphasize_sentences"],
            sentence_count=len(sentences),
            minimum=1,
            maximum_items=1,
        ),
        short_instruction="",
        profile=_MUSIC_PROFILE,
    )
