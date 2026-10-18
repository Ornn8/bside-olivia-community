"""English text from the writers passes the checks that were sized for Chinese; Chinese checks stay as they were."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError

from runtime import ui_language
from runtime.diary import diary
from runtime.diary.diary import DiaryStore, SHANGHAI, validate, write_day
from runtime.media import song_content
from runtime.media.song_content import SongSemanticPlan, _planner_contract, parse_song_semantic_plan
from runtime.media.song_plan_cache import cached_song_plan
from runtime.personal_chat.daily_video import select_candidate
from runtime.personal_chat.service import chat_text
from runtime.personal_chat.speech import validate_script
from runtime.reply.letter_presentation import split_signature
from runtime.reply.reply_context import OutputConstraints, ReplyMode
from runtime.reply.reply_policy import ViolationCode, scan_reply
from tests.media.test_song_semantic_plan import _payload, _short_lyrics
from tests.persona.test_reply_model_quality import _context


def english():
    ui_language.set_language("en")


# --- letters: the length limit counts content, not characters -------------------------------------------------------

def test_a_long_english_letter_is_within_the_limit_that_a_chinese_letter_of_the_same_size_meets():
    letter = ("I read your letter twice on the way home. " * 55).strip()  # about 2,300 characters, ~400 words
    assert OutputConstraints.for_mode(ReplyMode.TEXT_LETTER).max_characters == 1200
    codes = {item.code for item in scan_reply(letter, _context()).violations}
    assert ViolationCode.OUTPUT_LIMIT_EXCEEDED in codes
    english()
    assert OutputConstraints.for_mode(ReplyMode.TEXT_LETTER).max_characters == 4800
    assert ViolationCode.OUTPUT_LIMIT_EXCEEDED not in {item.code for item in scan_reply(letter, _context()).violations}
    assert ViolationCode.OUTPUT_LIMIT_EXCEEDED in {item.code for item in scan_reply(letter * 3, _context()).violations}


# --- spoken stories --------------------------------------------------------------------------------------------------

def script(length):
    return {"title": "A story", "spoken_text": "x" * length, "continuation_summary": "a summary"}


def test_a_ten_minute_english_story_fits_the_script_limit():
    assert validate_script(script(8000))["spoken_text"]
    with pytest.raises(ValueError):
        validate_script(script(9700))
    english()
    assert validate_script(script(9700))["spoken_text"]
    with pytest.raises(ValueError):
        validate_script(script(32001))
    with pytest.raises(ValueError):
        validate_script(script(39))


# --- chat text ---------------------------------------------------------------------------------------------------------

def test_chinese_chat_text_is_unchanged_and_english_keeps_its_periods():
    assert chat_text("好呀。我也想你。 ") == "好呀\n我也想你"
    assert chat_text("好呀... 我在.") == "好呀... 我在"
    assert chat_text("I missed you. Did you sleep well?") == "I missed you Did you sleep well?"  # the old rule, in Chinese mode
    english()
    assert chat_text("I missed you. Did you sleep well? Wait... yes. ") == "I missed you. Did you sleep well? Wait... yes."
    assert chat_text("好呀。我也想你。") == "好呀\n我也想你"


# --- letter signature -------------------------------------------------------------------------------------------------

def test_the_default_signature_is_linli_in_english_and_unchanged_in_chinese():
    assert split_signature("Dear you,\n\nBe well.") == ("Dear you,\n\nBe well.", "林离")
    assert split_signature("亲爱的你，\n\n保重。\n林离") == ("亲爱的你，\n\n保重。", "林离")
    english()
    assert split_signature("Dear you,\n\nBe well.") == ("Dear you,\n\nBe well.", "Linli")
    assert split_signature("Dear you,\n\nBe well.\n\n—Linli") == ("Dear you,\n\nBe well.", "Linli")
    assert split_signature("Dear you,\n\nBe well.\n\n[[signature:Little cat]]") == ("Dear you,\n\nBe well.", "Little cat")
    assert split_signature("Dear you,\n\nBe well.\nLinli\n[[signature:Linli]]") == ("Dear you,\n\nBe well.", "Linli")
    # the prompt's own default is the Chinese name; in English the letter is signed with the English one
    assert split_signature("Dear you,\n\nBe well.\n[[signature:林离]]") == ("Dear you,\n\nBe well.", "Linli")


# --- the contact invitation names both apps -----------------------------------------------------------------------------

def test_an_english_invitation_may_name_wechat_in_latin_letters():
    import local_server
    assert local_server._names_wechat("QQ 还是 微信？")
    assert not local_server._names_wechat("Would you like QQ or WeChat?")
    english()
    assert local_server._names_wechat("Would you like QQ or WeChat?")
    assert local_server._names_wechat("QQ 还是 微信？")
    assert not local_server._names_wechat("Would you like QQ?")


# --- daily video words ---------------------------------------------------------------------------------------------------

def test_daily_video_words_have_english_room():
    candidate = {"event_id": "e1", "version": 1, "target_date": "2026-10-07", "event_at": "2026-10-07T12:00:00+08:00",
                 "event_kind": "walk", "scene_id": "park", "certainty": "live"}
    spoken = "Look, I just woke up and I am still a bit groggy " * 3  # about 150 characters, over the Chinese limit of 100
    with pytest.raises(ValueError):
        select_candidate({"event_id": "e1", "spoken_text": spoken}, [candidate])
    english()
    assert select_candidate({"event_id": "e1", "spoken_text": spoken}, [candidate])["spoken_text"] == spoken.strip()


# --- the diary ---------------------------------------------------------------------------------------------------------

NOW = datetime(2026, 10, 8, 4, 0, tzinfo=SHANGHAI)
DAY = datetime(2026, 10, 7, tzinfo=SHANGHAI)
ROWS = [
    {"letter_id": "q1", "channel": "qq", "content": "I have my music theory exam next Wednesday and I am so nervous",
     "reply_text": "I will be with you", "delivery_status": "DELIVERED",
     "life_received_at": (DAY + timedelta(hours=20)).isoformat()},
    {"letter_id": "l1", "content": "Do you remember August 26?", "reply_text": "Of course", "letter_status": "COMPLETED",
     "life_received_at": (DAY + timedelta(hours=9)).isoformat()},
]
ENGLISH_ENTRY = {
    "title": "The exam you are nervous about",  # 30 characters: over the Chinese limit of 20
    "mood": "tender and worried",  # 18 characters: over 8
    "body": ("You told me about next Wednesday and I could hear how nervous you were. " * 40).strip(),  # ~2,800 characters
    "facts": [{"kind": "user_event", "text": "The user has a music theory exam on Wednesday", "date": "2026-10-14",
               "quote": "my music theory exam next Wednesday"}],
}


class Gateway:
    def __init__(self, *values):
        self.values, self.calls = list(values), []
        self.config = SimpleNamespace(model="claude-sonnet-5-5")

    async def complete_structured_scoped(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return SimpleNamespace(text=json.dumps(self.values[min(len(self.calls), len(self.values)) - 1], ensure_ascii=False))


def test_chinese_diary_uses_exactly_the_original_formats(tmp_path):
    entry = {"title": "下周三的乐理", "mood": "心疼", "body": "你说下周三要考乐理……", "facts": []}
    gateway = Gateway(entry, {k: entry[k] for k in ("title", "mood", "body")})
    assert asyncio.run(write_day(DiaryStore(tmp_path / "d.sqlite3"), gateway, "2026-10-07", ROWS, persona="p", now=NOW)) == "written"
    assert gateway.calls[0][1]["response_format"] == diary.FORMAT
    assert gateway.calls[0][1]["request_id"] == "diary:2026-10-07"
    assert gateway.calls[1][1]["response_format"] == diary.REVISE_FORMAT
    assert gateway.calls[1][1]["request_id"] == "diary-revise:2026-10-07"
    with pytest.raises(ValidationError):
        validate(ENGLISH_ENTRY, diary.day_messages(ROWS, "2026-10-07"))


def test_english_diary_has_room_and_asks_through_user_facing_requests(tmp_path):
    from runtime import output_language
    english()
    messages = diary.day_messages(ROWS, "2026-10-07")
    assert validate(ENGLISH_ENTRY, messages)["title"] == ENGLISH_ENTRY["title"]
    revised = {"title": "Next Wednesday", "mood": "tender", "body": ("You told me about your exam. " * 60).strip()}
    gateway = Gateway(ENGLISH_ENTRY, revised)
    store = DiaryStore(tmp_path / "d.sqlite3")
    assert asyncio.run(write_day(store, gateway, "2026-10-07", ROWS, persona="p", now=NOW)) == "written"
    assert gateway.calls[0][1]["response_format"] == diary._ENGLISH_FORMAT
    assert gateway.calls[0][1]["response_format"]["json_schema"]["schema"]["properties"]["body"]["maxLength"] == 6000
    assert [call[1]["request_id"] for call in gateway.calls] == ["diary:2026-10-07", "diary-revise:2026-10-07"]
    assert all(output_language.writes_for_user(call[1]["request_id"]) for call in gateway.calls)
    saved = store.entry("2026-10-07")
    assert saved["title"] == "Next Wednesday"  # the revision was accepted: its length is judged in words
    assert saved["body"].startswith("You told me about your exam.")


def test_an_english_revision_that_loses_most_of_the_text_is_not_accepted(tmp_path):
    english()
    short = {"title": "Next Wednesday", "mood": "tender", "body": "You told me about your exam."}
    store = DiaryStore(tmp_path / "d.sqlite3")
    asyncio.run(write_day(store, Gateway(ENGLISH_ENTRY, short), "2026-10-07", ROWS, persona="p", now=NOW))
    assert store.entry("2026-10-07")["title"] == ENGLISH_ENTRY["title"]  # the draft stays


def test_the_diary_length_check_is_unchanged_in_chinese_and_counts_words_in_english():
    assert diary._length_in_range("字" * 300, (250, 600))
    assert not diary._length_in_range("字" * 100, (250, 600))
    assert not diary._length_in_range("word " * 200, (250, 600))  # 800 characters are too long in Chinese mode
    english()
    assert diary._length_in_range("word " * 200, (250, 600))
    assert not diary._length_in_range("word " * 30, (250, 600))
    assert not diary._length_in_range("word " * 600, (250, 600))


# --- original song lyrics -------------------------------------------------------------------------------------------------

def english_lyrics(duration, line="I keep the lamp on for you tonight"):
    verse_count, chorus_count = {40: (6, 6), 60: (8, 8), 110: (10, 10)}[duration]
    return "\n".join(("[Intro]", "[Verse]", *([line] * verse_count), "[Chorus]", *([line] * chorus_count), "[Outro]"))


def test_chinese_lyrics_keep_their_checks_and_english_lyrics_are_refused_in_chinese_mode():
    assert song_content._validate_semantic_lyrics(_short_lyrics(40), 40) == _short_lyrics(40)
    with pytest.raises(ValueError, match="LANGUAGE_INVALID"):
        song_content._validate_semantic_lyrics(english_lyrics(40, "Hold me close"), 40)
    with pytest.raises(ValueError, match="LINE_LENGTH_INVALID"):
        song_content._validate_semantic_lyrics(_short_lyrics(40).replace("主歌第1句轻轻落下", "字" * 25), 40)


def test_english_lyrics_are_accepted_in_english_and_chinese_ones_still_are():
    english()
    lyrics = english_lyrics(40, "Hold the light a little longer")
    assert song_content._validate_semantic_lyrics(lyrics, 40) == lyrics
    assert song_content._validate_semantic_lyrics(_short_lyrics(40), 40) == _short_lyrics(40)  # a saved plan
    longest = english_lyrics(40, "x" * 48)
    assert song_content._validate_semantic_lyrics(longest, 40) == longest
    with pytest.raises(ValueError, match="LINE_LENGTH_INVALID"):
        song_content._validate_semantic_lyrics(english_lyrics(40, "x" * 49), 40)
    with pytest.raises(ValueError, match="LINE_COUNT_INVALID"):
        song_content._validate_semantic_lyrics(english_lyrics(60), 40)
    plan = parse_song_semantic_plan(json.dumps({**_payload(40), "lyrics": lyrics}), 40)
    assert isinstance(plan, SongSemanticPlan) and plan.lyrics == lyrics


def test_the_planner_contract_asks_for_the_language_of_the_interface():
    chinese = _planner_contract(110)
    assert "Write exactly 20 original Simplified Chinese lyric lines: 10 in Verse and 10 in Chorus." in chinese
    assert "- Each lyric line must contain four to twenty-four non-whitespace characters." in chinese
    english()
    contract = _planner_contract(110)
    assert "Write exactly 20 original English lyric lines: 10 in Verse and 10 in Chorus." in contract
    assert "- Each lyric line must contain four to forty-eight non-whitespace characters." in contract
    assert contract.replace("English", "Simplified Chinese").replace("forty-eight", "twenty-four") == chinese
    assert "Simplified Chinese" not in contract


def test_a_saved_song_plan_is_not_reused_across_languages(tmp_path):
    from runtime.media.song_content import SongContentPlan
    path = tmp_path / "song-plan.private.json"
    planned = []

    def planner(lyrics):
        def plan():
            planned.append(lyrics)
            semantic = parse_song_semantic_plan(json.dumps({**_payload(40), "lyrics": lyrics}), 40)
            from runtime.media.music_caption import render_minimax_caption
            return SongContentPlan(semantic.emotion_arc.value, semantic.lyrics, render_minimax_caption(semantic), 40,
                                   semantic_plan=semantic, suno_style="")
        return plan

    cached_song_plan(path, "letter", "reply", 40, planner(_short_lyrics(40)))
    assert path.read_text(encoding="utf-8")
    expected = __import__("hashlib").sha256(json.dumps(["letter", "reply", 40], ensure_ascii=False,
                                                       separators=(",", ":")).encode()).hexdigest()
    assert json.loads(path.read_text(encoding="utf-8"))["input_sha256"] == expected  # Chinese identity is unchanged
    cached_song_plan(path, "letter", "reply", 40, planner("never used"))
    assert planned == [_short_lyrics(40)]  # reused in Chinese
    english()
    cached_song_plan(path, "letter", "reply", 40, planner(english_lyrics(40, "Hold the light a little longer")))
    assert len(planned) == 2  # asked again in English
    assert json.loads(path.read_text(encoding="utf-8"))["input_sha256"] != expected


# --- a letter through the real orchestrator and gateway -------------------------------------------------------------------

def letter_through_the_pipeline(monkeypatch, provider_text):
    """Run the reply pipeline with the real orchestrator and gateway against a local provider; returns (result, requests)."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from llm_gateway import OpenAICompatibleAdapter
    from reply_orchestrator import ReplyOrchestrator, ReplyRequest
    from runtime.reply.reply_context import ReplyContext, TrustedTime
    from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
    from runtime.reply.reply_reviewer import NullReviewer
    from tests.llm.test_gateway import make_config

    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    seen = []

    async def provider(request):
        seen.append(await request.json())
        return web.json_response({"choices": [{"finish_reason": "stop", "message": {"content": provider_text}}]})

    async def run():
        app = web.Application()
        app.router.add_post("/v1/chat/completions", provider)
        async with TestClient(TestServer(app)) as client:
            gateway = OpenAICompatibleAdapter(make_config(str(client.make_url("/v1"))))
            pipeline = ReplyPipeline(ReplyOrchestrator(gateway), reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                                     discover_runtime_ports=False)
            context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=TrustedTime(NOW))
            request = ReplyRequest(
                content="synthetic letter", request_id="letter-reply:synthetic", max_input_chars=40000,
                messages=({"role": "system", "content": "persona"}, {"role": "user", "content": "synthetic letter"}))
            return await pipeline.run(request, context)

    return asyncio.run(run()), seen


ENGLISH_LETTER = ("Dear you,\n\nI read your letter twice on the way home, and the part about the exam stayed with me. "
                  "Tell me how Wednesday goes.\n\n[[sticker:linli-01]]\n[[signature:Linli]]")


def test_an_english_letter_keeps_its_markers_and_signature_through_the_pipeline(monkeypatch):
    from runtime.output_language import DIRECTIVE
    english()
    result, seen = letter_through_the_pipeline(monkeypatch, ENGLISH_LETTER)
    assert result.state.value == "completed", result
    assert result.text.startswith("Dear you,") and result.text.endswith("Tell me how Wednesday goes.")
    assert "[[" not in result.text
    assert (result.signature, result.sticker_id) == ("Linli", "linli-01")
    assert len(seen) == 1  # the directive is part of the writer call, never a second call
    sent = [message["content"] for message in seen[0]["messages"]]
    assert sent.count(DIRECTIVE) == 1 and sent[-1] == "synthetic letter" and sent[-2] == DIRECTIVE


def test_the_same_letter_in_chinese_sends_no_directive_and_signs_with_the_default(monkeypatch):
    from runtime.output_language import DIRECTIVE
    result, seen = letter_through_the_pipeline(monkeypatch, "亲爱的你，\n\n我读了两遍。\n\n[[sticker:linli-01]]")
    assert result.state.value == "completed", result
    assert (result.text, result.signature, result.sticker_id) == ("亲爱的你，\n\n我读了两遍。", "林离", "linli-01")
    assert all(DIRECTIVE not in message["content"] for message in seen[0]["messages"])


def test_an_english_qq_reply_keeps_its_envelope_and_quotes_through_the_pipeline(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from llm_gateway import GatewayRequestScope, OpenAICompatibleAdapter
    from reply_orchestrator import ReplyOrchestrator, ReplyRequest
    from runtime.output_language import DIRECTIVE
    from runtime.personal_chat.presentation import CURRENT
    from runtime.reply.reply_context import ReplyContext, TrustedTime
    from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
    from runtime.reply.reply_reviewer import NullReviewer
    from tests.llm.test_gateway import make_config

    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    user = "Let's talk tomorrow, I am going to sleep now"
    envelope = {"text": "Sleep well. I will be here tomorrow.", "delivery": "text", "text_reason": None, "listening": "keep",
                "initiative": "keep", "pause_until": None, "letter": "keep", "letter_until": None, "followup_at": None,
                "evidence": "", "sticker": None, "skip": False}
    seen = []

    async def provider(request):
        seen.append(await request.json())
        return web.json_response({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(envelope)}}]})

    async def run():
        app = web.Application()
        app.router.add_post("/v1/chat/completions", provider)
        async with TestClient(TestServer(app)) as client:
            gateway = OpenAICompatibleAdapter(make_config(str(client.make_url("/v1"))))
            pipeline = ReplyPipeline(ReplyOrchestrator(gateway), reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                                     discover_runtime_ports=False)
            context = ReplyContext.create(ReplyMode.FUTURE_IM, trusted_time=TrustedTime(NOW), future_im_enabled=True)
            request = ReplyRequest(
                content=user, request_id="personal-chat:ex1:1", max_input_chars=40000,
                gateway_scope=GatewayRequestScope.PERSONAL_CHAT_JSON,
                messages=({"role": "system", "content": "persona"}, {"role": "user", "content": user}))
            token = CURRENT.set({"structured": True, "raw_user_text": user, "channel": "qq", "speech_enabled": False,
                                 "semantic_kinds": ["text"], "decision_now": "2026-10-07T20:00:00+08:00",
                                 "received_source_id": "reply:ex1:user", "proactive": False})
            try:
                return await pipeline.run(request, context)
            finally:
                CURRENT.reset(token)

    english()
    result = asyncio.run(run())
    assert result.state.value == "completed", result
    assert json.loads(result.text)["text"] == "Sleep well. I will be here tomorrow."
    assert len(seen) == 1
    sent = [message["content"] for message in seen[0]["messages"]]
    assert sent.count(DIRECTIVE) == 1 and sent[-1] == user and sent[-2] == DIRECTIVE
