from __future__ import annotations

import pytest

from original_client_letter_contract import (
    OriginalClientAuditStatus,
    OriginalClientContractError,
    OriginalClientLetterStatus,
    OriginalClientReplyType,
    serialize_letter_detail,
    serialize_letter_list,
    serialize_letter_summary,
    serialize_unread_count,
)


NOW = 1_800_000_000.0


def test_explicit_unknown_history_time_is_not_replaced_by_current_time():
    summary = serialize_letter_summary(_letter(created_at=None, replied_at=None), now=NOW)
    assert summary["createdAt"] is None


def _letter(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "letter_id": "letter-fixture-001",
        "content": "这是一封用于原版信箱契约测试的合成来信。",
        "letter_status": "COMPLETED",
        "audit_status": 2,
        "is_read": 1,
        "created_at": 1_700_000_000,
        "replied_at": 1_700_000_100,
        "reply_text": "这是已经通过质量门的合成回复。",
        "reply_mode": "text_letter",
        "media_status": "NOT_REQUESTED",
        "reply_not_before": 0.0,
        "material": {"stamp_id": "stamp-1", "paperId": "paper-2"},
    }
    value.update(overrides)
    return value


def test_original_client_enums_match_supported_bundle() -> None:
    assert tuple(int(value) for value in OriginalClientLetterStatus) == (1, 2, 3, 4, 5)
    assert tuple(int(value) for value in OriginalClientAuditStatus) == (1, 2, 3)
    assert tuple(int(value) for value in OriginalClientReplyType) == (0, 1, 2, 3, 4)


def test_pending_publication_hides_reply_and_uses_original_pending_status() -> None:
    detail = serialize_letter_detail(
        _letter(reply_not_before=NOW + 60),
        now=NOW,
    )

    assert detail == {
        "letterId": "letter-fixture-001",
        "isRead": 1,
        "letterStatus": 1,
        "auditStatus": 2,
        "summary": "这是一封用于原版信箱契约测试的合成来信。",
        "createdAt": 1_700_000_000,
        "repliedAt": 1_700_000_100,
        "replyType": 0,
        "material": {"stampId": "stamp-1", "paperId": "paper-2"},
        "content": "这是一封用于原版信箱契约测试的合成来信。",
        "replyText": "",
        "replyVideoUrl": "",
    }


def test_text_reply_uses_original_text_type() -> None:
    detail = serialize_letter_detail(_letter(), now=NOW)

    assert detail["letterStatus"] == OriginalClientLetterStatus.REPLIED
    assert detail["replyType"] == OriginalClientReplyType.TEXT
    assert detail["replyText"] == "这是已经通过质量门的合成回复。"
    assert detail["replyVideoUrl"] == ""


def test_video_modes_fall_back_to_durable_text_until_media_is_complete() -> None:
    for mode in ("spoken_video", "musical_video"):
        detail = serialize_letter_detail(
            _letter(
                reply_mode=mode,
                media_status="PROCESSING",
                reply_video_url="http://127.0.0.1:8899/toy/media/letter-fixture-001.mp4",
            ),
            now=NOW,
        )
        assert detail["letterStatus"] == 4
        assert detail["replyType"] == 1
        assert detail["replyText"] == "这是已经通过质量门的合成回复。"
        assert detail["replyVideoUrl"] == ""


def test_completed_legacy_spoken_video_is_projected_as_the_musical_video() -> None:
    detail = serialize_letter_detail(
        _letter(
            reply_mode="spoken_video",
            media_status="COMPLETED",
            reply_video_url="http://127.0.0.1:8899/toy/media/letter-fixture-001.mp4",
        ),
        now=NOW,
    )

    assert detail["letterStatus"] == 4
    assert detail["replyType"] == 4
    assert detail["replyVideoUrl"] == (
        "http://127.0.0.1:8899/toy/media/letter-fixture-001.mp4"
    )


def test_completed_musical_video_uses_original_singing_mix_type() -> None:
    detail = serialize_letter_detail(
        _letter(
            reply_mode="musical_video",
            media_status="COMPLETED",
            reply_video_url="http://localhost:8899/toy/media/letter-fixture-001.mp4",
        ),
        now=NOW,
    )

    assert detail["replyType"] == 4
    assert detail["replyVideoUrl"] == (
        "http://localhost:8899/toy/media/letter-fixture-001.mp4"
    )


def test_untrusted_or_malformed_media_urls_never_reach_original_client() -> None:
    values = (
        "https://example.invalid/video.mp4",
        "http://127.0.0.1:8899/private/video.mp4",
        "http://127.0.0.1:8899/toy/media/../video.mp4",
        "http://user:password@127.0.0.1:8899/toy/media/video.mp4",
        "http://127.0.0.1:8899/toy/media/video.mp4?token=secret",
        "not-a-url",
    )
    for value in values:
        detail = serialize_letter_detail(
            _letter(
                reply_mode="spoken_video",
                media_status="COMPLETED",
                reply_video_url=value,
            ),
            now=NOW,
        )
        assert detail["replyType"] == 1
        assert detail["replyVideoUrl"] == ""


def test_failed_letter_preserves_original_failure_and_audit_enums() -> None:
    summary = serialize_letter_summary(
        _letter(
            letter_status="FAILED",
            audit_status="REJECTED",
            reply_text="",
            replied_at=None,
        ),
        now=NOW,
    )

    assert summary["letterStatus"] == 5
    assert summary["auditStatus"] == 3
    assert summary["replyType"] == 0
    assert "repliedAt" not in summary


def test_failed_letter_ignores_future_reply_publication_deadline() -> None:
    letter = _letter(
        letter_status="FAILED",
        reply_text="",
        replied_at=None,
        reply_not_before=NOW + 60,
    )

    summary = serialize_letter_summary(letter, now=NOW)
    detail = serialize_letter_detail(letter, now=NOW)

    assert summary["letterStatus"] == OriginalClientLetterStatus.FAILED
    assert detail["letterStatus"] == OriginalClientLetterStatus.FAILED
    assert detail["replyText"] == ""


def test_exact_aggregate_and_unread_names_are_emitted_with_optional_legacy_aliases() -> None:
    listing = serialize_letter_list(
        [_letter(), _letter(letter_id="letter-fixture-002", is_read=0)],
        remaining_today=99,
        scope="current",
        now=NOW,
        include_legacy_aliases=True,
    )
    unread = serialize_unread_count(
        1,
        scope="current",
        include_legacy_aliases=True,
    )

    assert listing["hasMore"] is False
    assert listing["nextCursor"] == 0
    assert listing["remainingToday"] == 99
    assert listing["has_more"] is False
    assert listing["next_cursor"] == 0
    assert listing["remaining_today"] == 99
    assert listing["list"][0]["letterId"] == "letter-fixture-001"
    assert listing["list"][0]["letter_id"] == "letter-fixture-001"
    assert unread == {
        "unreadCount": 1,
        "unread_count": 1,
        "scope": "current",
        "read_only": False,
    }


def test_detail_legacy_aliases_do_not_change_original_fields() -> None:
    detail = serialize_letter_detail(
        _letter(
            reply_mode="spoken_video",
            media_status="COMPLETED",
            reply_video_url="http://127.0.0.1:8899/toy/media/letter-fixture-001.mp4",
        ),
        now=NOW,
        include_legacy_aliases=True,
    )

    assert detail["letterId"] == detail["letter_id"]
    assert detail["letterStatus"] == detail["letter_status"] == 4
    assert detail["auditStatus"] == detail["audit_status"] == 2
    assert detail["replyType"] == detail["reply_type"] == 4
    assert detail["replyText"] == detail["reply_text"] == detail["reply_content"]
    assert detail["replyVideoUrl"] == detail["reply_video_url"]
    assert detail["reply_mode"] == "video"
    assert detail["reply_mode_exact"] == "musical_video"


def test_contract_rejects_invalid_ids_and_negative_counts() -> None:
    with pytest.raises(OriginalClientContractError) as invalid_id:
        serialize_letter_summary(_letter(letter_id=""), now=NOW)
    assert invalid_id.value.code == "ORIGINAL_CLIENT_LETTER_ID_INVALID"

    with pytest.raises(OriginalClientContractError) as invalid_remaining:
        serialize_letter_list([], remaining_today=-1, scope="current", now=NOW)
    assert invalid_remaining.value.code == "ORIGINAL_CLIENT_REMAINING_INVALID"

    with pytest.raises(OriginalClientContractError) as invalid_unread:
        serialize_unread_count(-1, scope="current")
    assert invalid_unread.value.code == "ORIGINAL_CLIENT_UNREAD_INVALID"
