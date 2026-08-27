"""Canonical non-spoken delivery planning for ordinary video replies.

The LLM reply remains the only spoken payload.  This module derives a small,
deterministic performance plan from that text so audio and video assembly can
share pacing and expression cues without ever inserting control prose into TTS.
"""

from __future__ import annotations

import re
from itertools import combinations
from dataclasses import asdict, dataclass


_BOUNDARY = re.compile(r"(?<=[。！？!?])")
_ANGRY = ("凭什么", "太过分", "不公平", "别再", "不该", "生气", "愤怒")
_HOPEFUL = (
    "如果愿意",
    "明天",
    "以后",
    "下次",
    "试着",
    "我听着",
    "慢慢",
    "会好",
    "热茶",
    "黄昏",
    "好么",
    "好吗",
)
_REASSURING = ("不是你的错", "不是你不够好", "不代表", "被替代的不是你", "不是帆不够好", "值得")
_SAD = ("难受", "失去", "空着", "离开", "被替代", "受伤", "痛", "孤单")
_ANXIOUS = ("害怕", "不安", "担心", "怀疑自己", "怕")


@dataclass(frozen=True)
class DeliveryCue:
    text: str
    emotion: str
    intensity: float
    speed: float
    pause_after_seconds: float
    expression: str
    motion: str


@dataclass(frozen=True)
class SpeechUnit:
    text: str
    cue_index: int
    speed: float
    pause_after_seconds: float


@dataclass(frozen=True)
class ReplyDeliveryPlan:
    cues: tuple[DeliveryCue, ...]
    duration_target_seconds: tuple[float, float] = (40.0, 50.0)
    source: str = "llm_reply_text"
    control_channel: str = "non_spoken"

    @property
    def spoken_text(self) -> str:
        return "".join(cue.text for cue in self.cues)

    @property
    def audio_instruction(self) -> str:
        """Describe one smooth delivery arc without changing the spoken text."""

        if not self.cues:
            return "像在私下回复熟人那样自然说话，保持正常、连贯、不拖沓的语速。"
        start = _EMOTION_INSTRUCTION[self.cues[0].emotion]
        end = _EMOTION_INSTRUCTION[self.cues[-1].emotion]
        return (
            "像在私下回复熟人那样自然说话，保持正常、连贯、不拖沓的语速。"
            f"整体情绪从{start}平缓过渡到{end}，依照正文含义自然起伏。"
            "句内保持连贯，只在标点确实需要时停顿。"
        )

    def speech_units(self) -> tuple[SpeechUnit, ...]:
        """Group complete sentences into two or three coherent acoustic blocks."""

        if not self.cues:
            return ()
        blocks = _semantic_blocks(tuple(cue.text for cue in self.cues))
        return tuple(
            SpeechUnit(text=text, cue_index=start, speed=1.0, pause_after_seconds=0.0)
            for start, text in blocks
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "control_channel": self.control_channel,
            "duration_target_seconds": list(self.duration_target_seconds),
            "cues": [asdict(cue) for cue in self.cues],
            "speech_units": [asdict(unit) for unit in self.speech_units()],
        }


_DELIVERY = {
    "empathetic": (0.56, 1.02, 0.30, "soft_concern", "small_listening_nod"),
    "sad": (0.58, 1.00, 0.34, "subdued_concern", "brief_downward_glance"),
    "anxious": (0.66, 1.04, 0.28, "focused_concern", "slight_forward_lean"),
    "reassuring": (0.76, 1.05, 0.26, "steady_reassurance", "single_firm_nod"),
    "angry": (0.82, 1.07, 0.24, "restrained_anger", "still_direct_gaze"),
    "hopeful": (0.60, 1.03, 0.38, "gentle_smile", "soft_head_tilt"),
}


_EMOTION_INSTRUCTION = {
    "empathetic": "克制的共情",
    "sad": "轻微的心疼",
    "anxious": "专注的关切",
    "reassuring": "温和的笃定",
    "angry": "克制的不平",
    "hopeful": "安静的希望",
}


def _semantic_blocks(sentences: tuple[str, ...]) -> tuple[tuple[int, str], ...]:
    """Partition sentence boundaries while avoiding both tiny and huge calls."""

    if not sentences:
        return ()
    total = sum(len(sentence.strip()) for sentence in sentences)
    desired = max(1, min(3, round(total / 60)))
    desired = min(desired, len(sentences))
    if desired == 1:
        return ((0, "".join(sentences)),)
    target = total / desired

    def score(boundaries: tuple[int, ...]) -> float:
        points = (0, *boundaries, len(sentences))
        lengths = [
            sum(len(sentence.strip()) for sentence in sentences[start:end])
            for start, end in zip(points, points[1:])
        ]
        bounds_penalty = sum(10_000 for length in lengths if length < 35 or length > 90)
        return bounds_penalty + sum((length - target) ** 2 for length in lengths)

    cuts = min(combinations(range(1, len(sentences)), desired - 1), key=score)
    points = (0, *cuts, len(sentences))
    return tuple(
        (start, "".join(sentences[start:end]))
        for start, end in zip(points, points[1:])
    )


_ORDINARY_VIDEO_CONSTRAINT = (
    "请生成一封可直接朗读的普通视频回信。只输出完整回信正文，不要标题、列表、括号说明、"
    "情绪标签、舞台提示或任何生成说明。按自然偏利落的普通话语速控制在40到50秒，"
    "正文严格为180到200个汉字，目标为190字。内容应先回应对方当下的感受，再给出清晰判断，最后自然收束；"
    "让语义本身有共情、坚定和希望的缓慢变化，不要复述整封来信。"
    "写成面对熟人说话的口语，使用六到七个完整句子，每句只表达一个意思；"
    "保留来信中的人物、地点和事实关系，不要把具体场景改写成不存在的画面。"
    "不要为了显得文艺而制造比喻、金句或压缩句意，也不要使用书面腔和心理学术语。"
)


def build_ordinary_video_llm_content(letter_content: str) -> str:
    """Add an output-only duration contract for the ordinary-video LLM call."""

    if not isinstance(letter_content, str) or not letter_content.strip():
        raise ValueError("REPLY_DELIVERY_TEXT_REQUIRED")
    return (
        letter_content.rstrip()
        + "\n\n<ordinary_video_reply_constraints>\n"
        + _ORDINARY_VIDEO_CONSTRAINT
        + "\n</ordinary_video_reply_constraints>"
    )


def ordinary_video_reply_length_ok(reply_text: str) -> bool:
    compact_length = len("".join(str(reply_text).split()))
    return 180 <= compact_length <= 200


def build_ordinary_video_repair_content(reply_text: str) -> str:
    """Ask the same product LLM for one bounded repair, never TTS controls."""

    return (
        "请将下面这封由你生成的回信改写为可直接朗读的完整正文。"
        "保留原意与林离口吻，严格控制在180到200个汉字，目标为190字，只输出正文；"
        "使用自然口语和六到七个完整句子，不要改变来信事实，不要生造比喻、金句或画面；"
        "不要标题、解释、括号说明、情绪标签或舞台提示。\n\n"
        + str(reply_text).strip()
    )


def _segments(text: str) -> tuple[str, ...]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("REPLY_DELIVERY_TEXT_REQUIRED")
    segments: list[str] = []
    start = 0
    for match in _BOUNDARY.finditer(text):
        end = match.end()
        if end > start:
            segments.append(text[start:end])
        start = end
    if start < len(text):
        segments.append(text[start:])
    return tuple(segment for segment in segments if segment)


def _emotion(segment: str, index: int, total: int) -> str:
    if any(marker in segment for marker in _ANGRY):
        return "angry"
    if any(marker in segment for marker in _HOPEFUL):
        return "hopeful"
    if any(marker in segment for marker in _REASSURING):
        return "reassuring"
    if any(marker in segment for marker in _ANXIOUS):
        return "anxious"
    if any(marker in segment for marker in _SAD):
        return "sad"
    if index == 0:
        return "empathetic"
    if index == total - 1 and total > 1:
        return "hopeful"
    return "reassuring" if index >= total // 2 else "empathetic"


def plan_reply_delivery(text: str) -> ReplyDeliveryPlan:
    """Derive sentence-level delivery cues from the final LLM reply text."""

    segments = _segments(text)
    cues: list[DeliveryCue] = []
    previous_speed: float | None = None
    for index, segment in enumerate(segments):
        emotion = _emotion(segment, index, len(segments))
        intensity, target_speed, pause, expression, motion = _DELIVERY[emotion]
        if previous_speed is None:
            speed = target_speed
        else:
            change = max(-0.02, min(0.02, target_speed - previous_speed))
            speed = round(previous_speed + change, 3)
        previous_speed = speed
        if segment.rstrip().endswith(("？", "?")):
            pause = min(0.42, pause + 0.04)
        cues.append(
            DeliveryCue(
                text=segment,
                emotion=emotion,
                intensity=intensity,
                speed=speed,
                pause_after_seconds=pause,
                expression=expression,
                motion=motion,
            )
        )
    return ReplyDeliveryPlan(tuple(cues))


__all__ = [
    "DeliveryCue",
    "ReplyDeliveryPlan",
    "SpeechUnit",
    "build_ordinary_video_llm_content",
    "build_ordinary_video_repair_content",
    "ordinary_video_reply_length_ok",
    "plan_reply_delivery",
]
