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
    tone_tier: str | None = None

    def __post_init__(self) -> None:
        if self.tier not in _TIERS or self.caution not in {"normal", "elevated", "high"}:
            raise ValueError("INITIATIVE_PROFILE_INVALID")
        if not 0 < self.im_interval_min <= self.im_interval_max:
            raise ValueError("INITIATIVE_PROFILE_INVALID")
        if not 1 <= self.im_attempt_limit <= 72 or not 1 <= self.letter_daily_limit <= 6:
            raise ValueError("INITIATIVE_PROFILE_INVALID")
        if self.letter_followup_delay <= 0 or (self.letter_silence_delay is not None and self.letter_silence_delay <= 0):
            raise ValueError("INITIATIVE_PROFILE_INVALID")

    @property
    def rank(self) -> int:
        return _TIERS.index(self.tier)

    @property
    def tone_rank(self) -> int:
        return _TIERS.index(self.tone_tier or self.tier)

    def public_view(self) -> dict[str, object]:
        """Safe behavioral projection; never expose hidden raw relationship scores."""
        return {
            "tier": self.tier,
            "caution": self.caution,
            "reason_floor": self.reason_floor,
        }


_BASE = {
    "reserved": InitiativeProfile(
        "reserved", "normal", 2 * 3600, 6 * 3600, 3,
        1, 12 * 3600, 3 * 86400,
        "可以基于具体的生活小事、上次话题或明确的关心主动联系；避免空洞寒暄、追问和施压。",
    ),
    "familiar": InitiativeProfile(
        "familiar", "normal", 1 * 3600, 3 * 3600, 6,
        2, 4 * 3600, 2 * 86400,
        "可以主动分享自己的生活小事、延续共同兴趣或上次话题，不需要任务型理由；避免连续追问。",
    ),
    "trusted": InitiativeProfile(
        "trusted", "normal", 30 * 60, 90 * 60, 12,
        2, 3 * 3600, 1 * 86400,
        "可以分享小事、自己的状态、轻量关心和自然吐槽，不要求每次都有任务型理由。",
    ),
    "close": InitiativeProfile(
        "close", "normal", 10 * 60, 25 * 60, 24,
        2, 90 * 60, 2 * 86400,
        "允许低信息量日常、随手分享、想念和没正事也想说两句；仍不给沉默施加解释义务。",
    ),
    "committed": InitiativeProfile(
        "committed", "normal", 5 * 60, 12 * 60, 72,
        6, 20 * 60, 4 * 3600,
        "有空时很想联系对方本身就可以成为理由，可自然表达想念、期待和想多聊几句；这是联系频率档位，不确认恋爱身份，也不授权催促、占有或监控。",
    ),
}


def _with_caution(profile: InitiativeProfile, caution: str) -> InitiativeProfile:
    from dataclasses import replace
    from runtime.cloud_policy import get_policy
    limits = get_policy('proactive')['intervals'][profile.tier]
    profile = replace(profile, im_interval_min=limits['min_minutes'] * 60,
                      im_interval_max=limits['max_minutes'] * 60)
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
        reason, profile.tone_tier,
    )


def tier_from_snapshot(snapshot) -> str:
    if snapshot is None:
        return "reserved"
    stage = str(getattr(snapshot, "relationship_stage", "unknown") or "unknown")
    if stage == "committed":
        return "committed"
    scores = [int(getattr(snapshot, name, 0) or 0)
              for name in ("familiarity", "trust", "comfort", "closeness")]
    # A behavioral cadence, never a write to relationship_stage or permission.
    familiarity, trust, comfort, closeness = scores
    if closeness >= 90 and trust >= 80 and comfort >= 80 and familiarity >= 70:
        return "committed"
    high = sum(value >= 70 for value in scores)
    medium = sum(value >= 35 for value in scores)
    if stage == "close" or high >= 3:
        return "close"
    if high >= 2:
        return "trusted"
    if stage == "familiar" or high >= 1 or medium >= 3:
        return "familiar"
    return "reserved"


def profile_from_snapshot(snapshot, history=None) -> InitiativeProfile:
    tier = tier_from_snapshot(snapshot)
    if history is not None:
        from runtime.memory.effective_relationship import effective_relationship_stage
        stage = effective_relationship_stage(snapshot, history)
        tier = {'familiar': 'familiar', 'friend': 'trusted', 'trusted_friend': 'trusted',
                'close': 'close', 'committed': 'committed'}.get(stage, 'reserved')
    tension = int(getattr(snapshot, "tension", 0) or 0) if snapshot is not None else 0
    caution = "high" if tension >= 70 else "elevated" if tension >= 35 else "normal"
    return _with_route(_with_caution(_BASE[tier], caution), getattr(snapshot, 'relationship_route', ''))


def _with_route(profile, chosen):
    from dataclasses import replace
    from runtime.cloud_policy import get_policy
    factor = get_policy('relationship_care').get(chosen, 100) / 100
    return replace(profile, im_interval_min=round(profile.im_interval_min * factor),
                   im_interval_max=round(profile.im_interval_max * factor),
                   letter_followup_delay=round(profile.letter_followup_delay * factor),
                   letter_silence_delay=round(profile.letter_silence_delay * factor) if profile.letter_silence_delay else None)


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
            return _with_route(_with_caution(_BASE[tier], caution), row.get('initiative_route', ''))
    for row in reversed(ordered):
        if row.get("contact_qualification") is True:
            return _with_caution(_BASE["close"], 'normal')
        if type(row.get("contact_qualification")) is bool:
            return _with_caution(_BASE["familiar"], 'normal')
    return _with_caution(_BASE["reserved"], 'normal')


def unanswered_wait(profile: InitiativeProfile, usual_gap: float | None, unanswered: int) -> float:
    """Return pressure-suppression time; silence later decays instead of locking forever."""
    if unanswered <= 0:
        return 0.0
    if profile.tier == "committed":
        # One unanswered everyday message does not imply hours of unavailability.
        # Repeated silence still progressively spaces contact; tension can add
        # further space after this ordinary-cadence ceiling.
        wait = 15 * 60 if unanswered == 1 else min(4 * 3600, 30 * 60 * 1.45 ** min(unanswered - 2, 12))
        return wait * (1.8 if profile.caution == 'high' else 1.35 if profile.caution == 'elevated' else 1)
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
