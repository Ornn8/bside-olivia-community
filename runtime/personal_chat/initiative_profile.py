"""Project relationship state into bounded proactive-behavior policy.

This module does not create another relationship score. It translates the
existing private-world relationship state into communication behavior: how
strong a reason is needed to initiate, how quickly another opportunity may be
considered, and how unanswered messages should suppress rather than permanently
lock future contact.
"""
from __future__ import annotations

from dataclasses import dataclass


_TIERS = ("reserved", "familiar", "trusted", "close", "committed")


@dataclass(frozen=True)
class InitiativeProfile:
    tier: str
    caution: str
    im_interval_min: int
    im_interval_max: int
    im_attempt_limit: int
    letter_daily_limit: int
    letter_followup_delay: int
    letter_silence_delay: int | None
    reason_floor: str

    def __post_init__(self) -> None:
        if self.tier not in _TIERS or self.caution not in {"normal", "elevated", "high"}:
            raise ValueError("INITIATIVE_PROFILE_INVALID")
        if not 0 < self.im_interval_min <= self.im_interval_max:
            raise ValueError("INITIATIVE_PROFILE_INVALID")
        if not 1 <= self.im_attempt_limit <= 12 or not 1 <= self.letter_daily_limit <= 3:
            raise ValueError("INITIATIVE_PROFILE_INVALID")
        if self.letter_followup_delay <= 0 or (self.letter_silence_delay is not None and self.letter_silence_delay <= 0):
            raise ValueError("INITIATIVE_PROFILE_INVALID")

    @property
    def rank(self) -> int:
        return _TIERS.index(self.tier)

    def public_view(self) -> dict[str, object]:
        """Safe behavioral projection; never expose hidden raw relationship scores."""
        return {
            "tier": self.tier,
            "caution": self.caution,
            "reason_floor": self.reason_floor,
        }


_BASE = {
    "reserved": InitiativeProfile(
        "reserved", "normal", 6 * 3600, 18 * 3600, 2,
        1, 24 * 3600, None,
        "需要明确的未完话题、约定或具体关心理由，不为表现主动而闲聊。",
    ),
    "familiar": InitiativeProfile(
        "familiar", "normal", 2 * 3600, 6 * 3600, 4,
        1, 8 * 3600, 7 * 86400,
        "可以主动延续共同兴趣、上次话题或具体生活关联；纯粹没事找话仍应克制。",
    ),
    "trusted": InitiativeProfile(
        "trusted", "normal", 45 * 60, 2 * 3600, 6,
        2, 3 * 3600, 4 * 86400,
        "可以分享小事、自己的状态、轻量关心和自然吐槽，不要求每次都有任务型理由。",
    ),
    "close": InitiativeProfile(
        "close", "normal", 20 * 60, 75 * 60, 9,
        2, 90 * 60, 2 * 86400,
        "允许低信息量日常、随手分享、想念和没正事也想说两句；仍不给沉默施加解释义务。",
    ),
    "committed": InitiativeProfile(
        "committed", "normal", 15 * 60, 60 * 60, 10,
        3, 45 * 60, 30 * 3600,
        "想联系对方本身可以成为理由，也可表达想念、期待或轻微失落；亲密不等于催促、占有或监控。",
    ),
}


def _with_caution(profile: InitiativeProfile, caution: str) -> InitiativeProfile:
    if caution == "normal":
        return profile
    multiplier = 2.0 if caution == "high" else 1.5
    reason = profile.reason_floor + (
        " 当前关系有明显紧张，优先留空间或修复，减少随意追问和连续主动。"
        if caution == "high" else
        " 当前关系有一些紧张，主动内容应更有理由并给对方退出空间。"
    )
    return InitiativeProfile(
        profile.tier, caution,
        round(profile.im_interval_min * multiplier),
        round(profile.im_interval_max * multiplier),
        max(1, profile.im_attempt_limit - (3 if caution == "high" else 1)),
        max(1, profile.letter_daily_limit - (1 if caution == "high" else 0)),
        round(profile.letter_followup_delay * multiplier),
        round(profile.letter_silence_delay * multiplier) if profile.letter_silence_delay else None,
        reason,
    )


def tier_from_snapshot(snapshot) -> str:
    if snapshot is None:
        return "reserved"
    stage = str(getattr(snapshot, "relationship_stage", "unknown") or "unknown")
    if stage == "committed":
        return "committed"
    scores = [int(getattr(snapshot, name, 0) or 0)
              for name in ("familiarity", "trust", "comfort", "closeness")]
    high = sum(value >= 70 for value in scores)
    medium = sum(value >= 35 for value in scores)
    if stage == "close" or high >= 3 or scores[3] >= 70:
        return "close"
    if high >= 2 or (scores[1] >= 70 and scores[2] >= 70):
        return "trusted"
    if stage == "familiar" or high >= 1 or medium >= 3:
        return "familiar"
    return "reserved"


def profile_from_snapshot(snapshot) -> InitiativeProfile:
    tier = tier_from_snapshot(snapshot)
    tension = int(getattr(snapshot, "tension", 0) or 0) if snapshot is not None else 0
    caution = "high" if tension >= 70 else "elevated" if tension >= 35 else "normal"
    return _with_caution(_BASE[tier], caution)


def profile_from_rows(rows: list[dict]) -> InitiativeProfile:
    """Recover the latest persisted projection without reading hidden stores.

    New interaction rows persist the profile when relationship evidence is
    committed. Older installations fall back conservatively: an existing true
    contact qualification proves at least a close-enough relationship, while
    any other evidenced relationship interaction is treated as familiar.
    """
    ordered = sorted((row for row in rows if isinstance(row, dict)),
                     key=lambda row: float(row.get("created_at", 0) or 0))
    for row in reversed(ordered):
        tier = row.get("initiative_tier")
        caution = row.get("initiative_caution", "normal")
        if tier in _TIERS and caution in {"normal", "elevated", "high"}:
            return _with_caution(_BASE[tier], caution)
    for row in reversed(ordered):
        if row.get("contact_qualification") is True:
            return _BASE["close"]
        if type(row.get("contact_qualification")) is bool:
            return _BASE["familiar"]
    return _BASE["reserved"]


def unanswered_wait(profile: InitiativeProfile, usual_gap: float | None, unanswered: int) -> float:
    """Return pressure-suppression time; silence later decays instead of locking forever."""
    if unanswered <= 0:
        return 0.0
    first = {
        "reserved": (24 * 3600, 72 * 3600, 1.5),
        "familiar": (12 * 3600, 48 * 3600, 1.2),
        "trusted": (6 * 3600, 24 * 3600, 1.0),
        "close": (3 * 3600, 12 * 3600, .8),
        "committed": (2 * 3600, 8 * 3600, .65),
    }[profile.tier]
    floor, cap, factor = first
    if usual_gap is None:
        wait = floor
    else:
        wait = max(floor, min(cap, usual_gap * factor))
    if unanswered >= 2:
        second_floor = {
            "reserved": 72 * 3600,
            "familiar": 36 * 3600,
            "trusted": 18 * 3600,
            "close": 10 * 3600,
            "committed": 6 * 3600,
        }[profile.tier]
        wait = max(wait, second_floor) * (1.45 ** (unanswered - 2))
        wait = min(wait, 7 * 86400)
    if profile.caution == "elevated":
        wait *= 1.35
    elif profile.caution == "high":
        wait *= 1.8
    return wait
