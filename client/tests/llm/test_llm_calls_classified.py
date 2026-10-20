"""Every place that asks a model for text is classified: user-facing (English directive) or internal.

A new writer cannot be forgotten: a file that calls the gateway and is missing from the table below fails here, with
the steps to take.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from runtime import output_language

ROOT = Path(__file__).parents[2]

CALL = re.compile(
    r"\.(?:complete|complete_scoped|complete_with_tools|complete_structured_scoped|stream|stream_scoped)\("
    r"|getattr\([^)\n]*['\"](?:complete|complete_scoped|complete_with_tools|complete_structured_scoped|stream|stream_scoped)['\"]"
    r"|chat/completions"
)

# The calls whose text the user reads or hears. Their request ids (or scope) are in output_language.
USER_FACING = {
    "runtime/reply/reply_orchestrator.py": "the writer for letters and chat; the ids come from the callers below",
    "local_server.py": "letter-reply: (letter writer), proactive: (letters she starts), the adapter that carries both",
    "runtime/diary/diary.py": "diary entries and memoirs (diary:, diary-revise:, diary-memoir:, diary-memoir-revise:)",
    "runtime/media/song_content.py": "original song lyrics (scope song_content)",
    "runtime/personal_chat/daily_video_author.py": "words for a selfie clip (daily-video-author:, daily-video-share:)",
    "runtime/reply/reply_model_quality.py": "mixed: the rewrite replaces what the user reads and asks for English itself; "
                                           "the reviewers are internal",
}
# Calls that only feed the program. The user never reads their output, so they stay in Chinese.
INTERNAL = {
    "letter_triage.py": "route classifier",
    "runtime/private_world/addressing_judgment.py": "the daily reading of what they call each other; only verbatim terms are kept",
    "private_world_candidate.py": "world candidate analysis",
    "runtime/image_reply.py": "photo plan and the English picture description",
    "runtime/image_understanding.py": "describes the picture the user sent (a direct call to the relay)",
    "runtime/imports/historical_memory.py": "relationship batch over imported history",
    "runtime/media/voice_direction.py": "delivery directions for the voice model",
    "runtime/memory/history_selection.py": "which earlier messages to recall",
    "runtime/private_world/character_emotion_runtime.py": "her inner state",
    "runtime/private_world/daily_life_runtime.py": "world simulation and fact extraction",
    "runtime/private_world/day_plan.py": "her day plan",
    "runtime/reply/current_turn_interpretation.py": "reads the user's current message",
}
# Not model calls at all.
OTHER = {
    "llm_gateway.py": "the gateway itself",
    "original_client_setup_api.py": "setup wizard step, not a model",
    "tts/service.py": "speech synthesis stream, not a model",
}
# Where each user-facing request id is spelled, so the list in output_language cannot go stale.
PREFIX_SOURCES = {
    "letter-reply:": ("local_server.py", "letter-reply"),
    "personal-chat:": ("runtime/personal_chat/backend.py", "personal-chat"),
    "proactive:": ("local_server.py", "'proactive:'"),
    "diary:": ("runtime/diary/diary.py", "'diary'"),
    "diary-revise:": ("runtime/diary/diary.py", "'-revise:'"),
    "diary-memoir:": ("runtime/diary/diary.py", "'diary-memoir'"),
    "diary-memoir-revise:": ("runtime/diary/diary.py", "'-revise:'"),
    "daily-video-author:": ("runtime/personal_chat/daily_video_author.py", "daily-video-author"),
    "daily-video-share:": ("runtime/personal_chat/daily_video_author.py", "daily-video-share"),
}


def calling_files() -> set[str]:
    found = set()
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(("tests/", "tools/", "scripts/", "control_center/", "installer/", ".evidence/")):
            continue
        if CALL.search(path.read_text(encoding="utf-8")):
            found.add(relative)
    return found


def test_every_file_that_calls_a_model_is_classified():
    known = set(USER_FACING) | set(INTERNAL) | set(OTHER)
    unclassified = calling_files() - known
    assert not unclassified, (
        "These files call the text gateway. If the user reads or hears the answer, add the request id prefix (or "
        "scope) to runtime/output_language.py, make sure the answer's length limits and parsers work for English, and "
        "list the file in USER_FACING; otherwise list it in INTERNAL with what it is for: " + str(sorted(unclassified))
    )
    assert not known - calling_files(), "a classified file no longer calls a model: " + str(sorted(known - calling_files()))


def test_the_three_tables_do_not_overlap():
    assert not (set(USER_FACING) & set(INTERNAL) or set(USER_FACING) & set(OTHER) or set(INTERNAL) & set(OTHER))


@pytest.mark.parametrize("prefix", output_language.USER_REQUEST_PREFIXES)
def test_every_user_facing_prefix_is_spelled_where_it_is_built(prefix):
    assert prefix in PREFIX_SOURCES, "add the file that builds this request id to PREFIX_SOURCES"
    relative, stem = PREFIX_SOURCES[prefix]
    assert stem in (ROOT / relative).read_text(encoding="utf-8")


def test_the_song_lyrics_scope_is_the_one_song_content_call():
    assert output_language.USER_SCOPES == {"song_content"}
    source = (ROOT / "runtime/media/song_content.py").read_text(encoding="utf-8")
    assert "GatewayRequestScope.SONG_CONTENT" in source
    others = [relative for relative in calling_files()
              if relative != "runtime/media/song_content.py"
              and "GatewayRequestScope.SONG_CONTENT" in (ROOT / relative).read_text(encoding="utf-8")]
    assert others == ["llm_gateway.py"]


def test_one_central_hook_and_one_explicit_exception():
    """The gateway adds the directive; only the quality rewrite (which shares the reviewer's id) asks for itself."""
    hooks = {}
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(("tests/", "tools/", "scripts/", "control_center/", ".evidence/")):
            continue
        source = path.read_text(encoding="utf-8")
        for name in ("with_directive(", "for_user_text("):
            if f"output_language.{name}" in source:
                hooks.setdefault(name, []).append(relative)
    assert hooks == {"with_directive(": ["llm_gateway.py"], "for_user_text(": ["runtime/reply/reply_model_quality.py"]}
