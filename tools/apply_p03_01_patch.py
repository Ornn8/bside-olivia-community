"""One-shot repository patch for P03-01.

This file is executed by a temporary branch workflow and removed before merge.
It exists only because the connector exposes whole-file writes, while
`local_server.py` needs a small, reviewable in-place integration patch.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "local_server.py"


def replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected exactly one match, found {count}: {old[:80]!r}")
    return text.replace(old, new, 1)


def main() -> None:
    text = TARGET.read_text(encoding="utf-8")

    text = replace_once(
        text,
        'PORT = int(_os.environ.get("OLIVIA_PORT", "8899"))\nLLM_TIMEOUT_SECONDS = 30\n',
        '''PORT = int(_os.environ.get("OLIVIA_PORT", "8899"))
LLM_TIMEOUT_SECONDS = 30


def _exact_reply_mode(value: object) -> str:
    """Normalize legacy wire values without losing the new internal mode."""

    if isinstance(value, ReplyMode):
        return value.value
    normalized = str(value or "").strip().lower()
    if normalized in {"text", ReplyMode.TEXT_LETTER.value}:
        return ReplyMode.TEXT_LETTER.value
    if normalized == ReplyMode.SPOKEN_VIDEO.value:
        return ReplyMode.SPOKEN_VIDEO.value
    if normalized in {"video", ReplyMode.MUSICAL_VIDEO.value}:
        # Before P03 every video reply was rendered by the musical path.
        return ReplyMode.MUSICAL_VIDEO.value
    return ReplyMode.TEXT_LETTER.value


def _wire_reply_mode(value: object) -> str:
    exact = _exact_reply_mode(value)
    return "text" if exact == ReplyMode.TEXT_LETTER.value else "video"
''',
    )

    text = replace_once(
        text,
        '''            if name == "letters":
                for item in value:
                    if item.get("media_status") == "PROCESSING":
''',
        '''            if name == "letters":
                for item in value:
                    item["reply_mode"] = _exact_reply_mode(
                        item.get("reply_mode", ReplyMode.TEXT_LETTER.value)
                    )
                    if item.get("media_status") == "PROCESSING":
''',
    )

    text = replace_once(
        text,
        '''        "reply_mode": l.get("reply_mode", "text") if published else "text",
        "triage": l.get("triage", {"status": "unavailable"}),
''',
        '''        "reply_mode": _wire_reply_mode(l.get("reply_mode")) if published else "text",
        "reply_mode_exact": (
            _exact_reply_mode(l.get("reply_mode"))
            if published
            else ReplyMode.TEXT_LETTER.value
        ),
        "triage": l.get("triage", {"status": "unavailable"}),
''',
    )

    text = replace_once(
        text,
        '''            "reply_mode": l.get("reply_mode", "text") if reply_published else "text",
            "triage": l.get("triage", {"status": "unavailable"}),
''',
        '''            "reply_mode": (
                _wire_reply_mode(l.get("reply_mode"))
                if reply_published
                else "text"
            ),
            "reply_mode_exact": (
                _exact_reply_mode(l.get("reply_mode"))
                if reply_published
                else ReplyMode.TEXT_LETTER.value
            ),
            "triage": l.get("triage", {"status": "unavailable"}),
''',
    )

    text = replace_once(
        text,
        '''    if reply_mode != "text" or _os.environ.get("OLIVIA_REPLY_DELAY_ENABLED", "0").casefold() not in {"1", "true", "yes", "on"}:
''',
        '''    if (
        _exact_reply_mode(reply_mode) != ReplyMode.TEXT_LETTER.value
        or _os.environ.get("OLIVIA_REPLY_DELAY_ENABLED", "0").casefold()
        not in {"1", "true", "yes", "on"}
    ):
''',
    )

    text = replace_once(
        text,
        '''            "reply_mode": "text",
            "triage": {"status": "pending"},
''',
        '''            "reply_mode": ReplyMode.TEXT_LETTER.value,
            "triage": {"status": "pending"},
''',
    )

    start = text.index(
        "async def generate_reply(letter_id, content, *, idempotency_key=None):"
    )
    end = text.index("\nif __name__ == \"__main__\":", start)
    replacement = '''async def generate_reply(letter_id, content, *, idempotency_key=None):
    """Run one routed current-letter reply to its canonical terminal state."""

    letter = next(
        (item for item in store.letters if item["letter_id"] == letter_id),
        None,
    )
    if letter is None:
        return False

    decision = await emotion_triage.classify(content)
    exact_mode = _exact_reply_mode(decision.reply_mode)
    letter["triage"] = decision.to_dict()
    letter["reply_mode"] = exact_mode
    if exact_mode in {
        ReplyMode.SPOKEN_VIDEO.value,
        ReplyMode.MUSICAL_VIDEO.value,
    }:
        letter["media_status"] = "UNAVAILABLE_THIRD_PARTY_NOT_INSTALLED"
    _schedule_text_reply_delay(letter, exact_mode)
    _persist_store_state()

    try:
        request = ReplyRequest(
            content=content,
            request_id=letter_id,
            idempotency_key=idempotency_key,
            max_input_chars=LLM_CONFIG.max_input_chars,
        )
        result = await asyncio.wait_for(
            reply_pipeline.run(
                request,
                letters_adapter.build_reply_context(ReplyMode(exact_mode)),
            ),
            timeout=LLM_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        letter["letter_status"] = "CANCELED"
        letter["error_code"] = "LLM_CANCELLED"
        _safe_log("letter_cancelled")
        return False
    except asyncio.TimeoutError:
        letter["letter_status"] = "FAILED"
        letter["error_code"] = "LLM_TIMEOUT"
        _safe_log("letter_failed", error_code="LLM_TIMEOUT")
        return False
    except (ValueError, RuntimeError):
        letter["letter_status"] = "FAILED"
        letter["error_code"] = "LLM_UNAVAILABLE"
        _safe_log("letter_failed", error_code="LLM_UNAVAILABLE")
        return False

    if result.quality_status is not None:
        letter["quality_status"] = result.quality_status
        letter["quality_violation_codes"] = list(result.violation_codes)
    if result.state is not ReplyState.COMPLETED:
        public_code, _retryable = _public_llm_error(result.error_code)
        letter["letter_status"] = "FAILED"
        letter["error_code"] = public_code
        _safe_log("letter_failed", error_code=public_code)
        return False

    _prepare_private_world_delivery(letter, result.text)
    letter["reply_text"] = result.text
    letter["letter_status"] = "COMPLETED"
    _persist_store_state()
    _commit_private_world_letter(letter)
    _persist_store_state()

    if exact_mode in {
        ReplyMode.SPOKEN_VIDEO.value,
        ReplyMode.MUSICAL_VIDEO.value,
    }:
        letter["media_status"] = "PENDING"
        _schedule_media_job(letter_id, content, result.text, exact_mode)

    letters_adapter.remember_conversation(content, result.text)
    _safe_log("letter_completed", reply_mode=exact_mode)
    return True
'''
    text = text[:start] + replacement + text[end:]

    TARGET.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
