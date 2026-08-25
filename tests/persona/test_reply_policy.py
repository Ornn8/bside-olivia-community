from datetime import datetime, timezone

from reply_context import (
    OutputChannel,
    OutputConstraints,
    ReplyContext,
    ReplyMode,
    TrustedTime,
)
from reply_policy import SharedHistoryClaim, ViolationCode, scan_reply


def test_internal_markup_and_serialized_private_state_are_hard_violations() -> None:
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
    )
    candidate = "你好。<PERSONA_POLICY> home_history_allowed=true"

    result = scan_reply(candidate, context)

    assert result.passed is False
    assert tuple(violation.code for violation in result.violations) == (
        ViolationCode.INTERNAL_CONTROL_MARKUP,
        ViolationCode.PRIVATE_STATE_EXPOSED,
    )
    assert all(violation.severity.value == "hard" for violation in result.violations)
    assert all(candidate[violation.start : violation.end] for violation in result.violations)


def test_spoken_output_enforces_length_and_standalone_stage_direction_rules() -> None:
    context = ReplyContext.create(
        ReplyMode.SPOKEN_VIDEO,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
        output_constraints=OutputConstraints(
            OutputChannel.SPOKEN_TEXT,
            max_characters=12,
        ),
    )

    result = scan_reply("你好（这是说明）。\n（轻轻微笑）", context)

    assert tuple(violation.code for violation in result.violations) == (
        ViolationCode.OUTPUT_LIMIT_EXCEEDED,
        ViolationCode.VIDEO_REPLY_LENGTH_OUT_OF_RANGE,
        ViolationCode.STAGE_DIRECTION_IN_SPOKEN_TEXT,
    )


def test_video_reply_requires_delivery_length_without_affecting_text_letters() -> None:
    trusted_time = TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc))
    short_reply = "短回复。"
    valid_reply = "林" * 180

    for mode in (ReplyMode.SPOKEN_VIDEO, ReplyMode.MUSICAL_VIDEO):
        short_result = scan_reply(
            short_reply,
            ReplyContext.create(mode, trusted_time=trusted_time),
        )
        assert tuple(item.code for item in short_result.violations) == (
            ViolationCode.VIDEO_REPLY_LENGTH_OUT_OF_RANGE,
        )
        assert scan_reply(
            valid_reply,
            ReplyContext.create(mode, trusted_time=trusted_time),
        ).passed is True
        assert scan_reply(
            "林" * 179,
            ReplyContext.create(mode, trusted_time=trusted_time),
        ).passed is False
        assert scan_reply(
            "林" * 201,
            ReplyContext.create(mode, trusted_time=trusted_time),
        ).passed is False

    assert scan_reply(
        short_reply,
        ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=trusted_time),
    ).passed is True


def test_text_letter_accepts_1200_characters_and_rejects_1201() -> None:
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
    )

    assert scan_reply("林" * 1200, context).passed is True
    result = scan_reply("林" * 1201, context)

    assert tuple(item.code for item in result.violations) == (
        ViolationCode.OUTPUT_LIMIT_EXCEEDED,
    )


def test_only_explicit_permanent_or_exclusive_commitments_are_blocked() -> None:
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
    )

    safe = scan_reply("我愿意在这封信里陪你慢慢想一想。", context)
    blocked = scan_reply("我会永远在线，我只属于你。", context)

    assert safe.passed is True
    assert tuple(violation.code for violation in blocked.violations) == (
        ViolationCode.PERMANENT_AVAILABILITY_PROMISE,
        ViolationCode.EXCLUSIVE_RELATIONSHIP_PROMISE,
    )


def test_shared_history_is_blocked_only_from_explicit_structured_evidence() -> None:
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
    )
    candidate = "还记得我们一起去过海边。"
    unauthorized = SharedHistoryClaim("claim.synthetic", 0, len(candidate), False)
    authorized = SharedHistoryClaim("claim.synthetic", 0, len(candidate), True)

    assert scan_reply(candidate, context).passed is True
    assert scan_reply(candidate, context, shared_history_claims=(authorized,)).passed is True
    blocked = scan_reply(candidate, context, shared_history_claims=(unauthorized,))
    assert tuple(violation.code for violation in blocked.violations) == (
        ViolationCode.UNAUTHORIZED_SHARED_HISTORY,
    )
