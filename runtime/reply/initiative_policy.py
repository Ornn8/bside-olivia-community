"""Relationship-driven initiative policy shared by IM and proactive letters."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import median


@dataclass(frozen=True)
class InitiativeProfile:
    band: str
    allow_low_stakes: bool
    im_check_min_seconds: int
    im_check_max_seconds: int
    im_attempt_cap: int
    unanswered_limit: int
    unanswered_reset_seconds: int
    letter_daily_cap: int
    letter_followup_delay_seconds: int
    letter_casual_after_seconds: int | None
    motive_policy: str

    def public(self) -> dict:
        return asdict(self)


_PROFILES = {
    "reserved": InitiativeProfile(
        "reserved", False, 90 * 60, 180 * 60, 4, 1, 24 * 3600,
        3, 30 * 60, None,
        "Only initiate for a clear unfinished matter, promise, or concrete reason; silence itself is not a reason.",
    ),
    "familiar": InitiativeProfile(
        "familiar", False, 60 * 60, 120 * 60, 6, 1, 12 * 3600,
        3, 30 * 60, None,
        "Follow up on known interests or unfinished topics; small sharing is possible when it clearly connects to prior conversation.",
    ),
    "trusted": InitiativeProfile(
        "trusted", True, 30 * 60, 75 * 60, 8, 2, 8 * 3600,
        3, 30 * 60, 18 * 3600,
        "Ordinary life sharing, light teasing, checking in, or saying something simply because it reminded her of the user are natural.",
    ),
    "close": InitiativeProfile(
        "close", True, 20 * 60, 60 * 60, 10, 2, 4 * 3600,
        3, 30 * 60, 12 * 3600,
        "Low-stakes contact is normal: sharing small moments, wanting to talk, missing the user, or asking what they are doing may be enough.",
    ),
    "committed": InitiativeProfile(
        "committed", True, 15 * 60, 45 * 60, 12, 3, 3 * 3600,
        3, 30 * 60, 8 * 3600,
        "Everyday presence and emotional openness are normal; wanting contact can itself be a reason, while boundaries and the user's availability still win.",
    ),
}


def _stage(value) -> str:
    value = getattr(value, "value", value)
    return str(value or "unknown").strip().lower()


def profile_from_snapshot(snapshot) -> InitiativeProfile:
    if snapshot is None:
        return _PROFILES["reserved"]
    stage = _stage(getattr(snapshot, "relationship_stage", "unknown"))
    scores = [int(getattr(snapshot, name, 0) or 0)
              for name in ("familiarity", "trust", "comfort", "closeness")]
    high = sum(value >= 70 for value in scores)
    mean = sum(scores) / len(scores)

    if stage == "committed":
        band = "committed"
    elif stage in {"close", "trusted_friend"} or (getattr(snapshot, "closeness", 0) >= 70 and high >= 2):
        band = "close"
    elif stage == "friend" or high >= 3:
        band = "trusted"
    elif stage == "familiar" or high >= 1 or mean >= 35:
        band = "familiar"
    else:
        band = "reserved"
    return _PROFILES[band]


def profile_from_public(value: object) -> InitiativeProfile:
    if not isinstance(value, dict):
        return _PROFILES["reserved"]
    return _PROFILES.get(str(value.get("band", "")), _PROFILES["reserved"])


def _stamp(row: dict) -> float:
    value = row.get("created_at", 0)
    return float(value) if type(value) in (int, float) and value >= 0 else 0.0


def interaction_rhythm(rows: list[dict], now: float) -> dict:
    delivered = sorted(
        (row for row in rows if row.get("delivery_status") == "DELIVERED"),
        key=_stamp,
    )
    user_rows = [row for row in delivered if row.get("origin") != "proactive"]
    user_times = [_stamp(row) for row in user_rows if _stamp(row) > 0]
    gaps = [later - earlier for earlier, later in zip(user_times, user_times[1:])
            if 5 * 60 <= later - earlier <= 7 * 86400]
    typical_gap = median(gaps[-8:]) if len(gaps) >= 2 else None

    last_user_at = user_times[-1] if user_times else None
    last_proactive = next((row for row in reversed(delivered)
                           if row.get("origin") == "proactive"), None)
    last_proactive_at = _stamp(last_proactive) if last_proactive else None
    last_row = delivered[-1] if delivered else None
    awaiting_reply = bool(last_row and last_row.get("origin") == "proactive")
    silence_seconds = max(0.0, now - last_user_at) if last_user_at else None

    if silence_seconds is None:
        silence_state = "unknown"
    elif typical_gap is not None and silence_seconds >= max(typical_gap * 2.0, typical_gap + 2 * 3600):
        silence_state = "longer_than_usual"
    elif typical_gap is not None and silence_seconds >= typical_gap * 1.25:
        silence_state = "noticeable"
    else:
        silence_state = "within_usual_rhythm"

    return {
        "awaiting_reply": awaiting_reply,
        "silence_state": silence_state,
        "seconds_since_user_message": int(silence_seconds) if silence_seconds is not None else None,
        "typical_user_gap_seconds": int(typical_gap) if typical_gap is not None else None,
        "seconds_since_last_proactive": (
            int(max(0.0, now - last_proactive_at)) if last_proactive_at is not None else None
        ),
    }


def initiative_context(rows: list[dict], snapshot, now: float) -> dict:
    profile = profile_from_snapshot(snapshot)
    rhythm = interaction_rhythm(rows, now)
    last_proactive_gap = rhythm["seconds_since_last_proactive"]
    awaiting_reply = rhythm["awaiting_reply"]

    if awaiting_reply and last_proactive_gap is not None and last_proactive_gap < profile.unanswered_reset_seconds:
        contact_pressure = "hold_back"
    elif awaiting_reply:
        contact_pressure = "gentle_reopen"
    else:
        contact_pressure = "open"

    silence_state = rhythm["silence_state"]
    if silence_state == "longer_than_usual":
        if profile.band in {"close", "committed"}:
            emotional_cue = "missing_or_mild_concern"
        elif profile.band == "trusted":
            emotional_cue = "notice_and_mild_missing"
        elif profile.band == "familiar":
            emotional_cue = "notice"
        else:
            emotional_cue = "neutral"
    elif silence_state == "noticeable" and profile.band in {"trusted", "close", "committed"}:
        emotional_cue = "notice"
    else:
        emotional_cue = "neutral"

    return {
        "relationship_band": profile.band,
        "allow_low_stakes_contact": profile.allow_low_stakes,
        "motive_policy": profile.motive_policy,
        "contact_pressure": contact_pressure,
        "emotional_cue": emotional_cue,
        **rhythm,
    }
