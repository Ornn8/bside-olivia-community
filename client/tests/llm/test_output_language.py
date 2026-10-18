"""In English the writers whose text the user reads get a language directive; nothing else changes.

Chinese requests must stay exactly as they were, and calls that only feed the program (memory, world, routing,
judgments) never get the directive in either language.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from llm_gateway import GatewayRequestScope, OpenAICompatibleAdapter
from runtime import output_language, ui_language
from runtime.output_language import DIRECTIVE
from tests.llm.test_gateway import make_config

USER_FACING = (
    "letter-reply:3f2a", "letter-reply:3f2a:retry-1", "personal-chat:ex1:1", "personal-chat:ex1:input-2:1",
    "proactive:cand-1:plan", "proactive:cand-1:body", "diary:2026-10-07", "diary-revise:2026-10-07",
    "diary-memoir:2026-09", "diary-memoir-revise:2026-09", "daily-video-author:ab12", "daily-video-share:ab12",
)
INTERNAL = (
    # the recall step of a reply is not the reply
    "history-select:letter-reply:3f2a", "history-select:personal-chat:ex1:1", "history-select:proactive:cand-1:context",
    "quality-9f8e7d", "quality-9f8e7d:continuity_memory", "life:7c1a", "life:7c1a:conduct", "day-plan:2026-10-07",
    "emotion:5d2b", "current-turn-interpretation:aa11", "letter-reply-mode-router", "reply-photo-plan-p1",
    "reply-photo-description-p1", "private-world-candidate:l1:1", "image-observation:cc33", "5f4dcc3b5aa765d6",
    "personal-chat-ish", "",
)
MESSAGES = (
    {"role": "system", "content": "persona"},
    {"role": "user", "content": "earlier"},
    {"role": "assistant", "content": "earlier reply"},
    {"role": "user", "content": "now"},
)


def english():
    ui_language.set_language("en")


def contents(messages):
    return [message["content"] for message in messages]


# --- which calls count as user-facing -------------------------------------------------------------------------------

@pytest.mark.parametrize("request_id", USER_FACING)
def test_user_facing_requests_are_recognised(request_id):
    assert output_language.writes_for_user(request_id)


@pytest.mark.parametrize("request_id", (*INTERNAL, None))
def test_internal_requests_are_not(request_id):
    assert not output_language.writes_for_user(request_id)


def test_the_song_lyrics_call_is_recognised_by_its_scope():
    assert output_language.writes_for_user("0a1b2c3d", GatewayRequestScope.SONG_CONTENT)
    for scope in GatewayRequestScope:
        assert output_language.writes_for_user(None, scope) == (scope is GatewayRequestScope.SONG_CONTENT)


# --- where the directive goes -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("request_id", USER_FACING)
def test_english_adds_one_directive_before_the_last_user_message(request_id):
    english()
    result = output_language.with_directive(MESSAGES, request_id)
    assert contents(result) == ["persona", "earlier", "earlier reply", DIRECTIVE, "now"]
    assert result[3]["role"] == "system"
    assert contents(output_language.with_directive(result, request_id)) == contents(result)  # asked twice, added once


@pytest.mark.parametrize("request_id", (*USER_FACING, *INTERNAL))
def test_chinese_never_changes_the_messages(request_id):
    assert output_language.with_directive(MESSAGES, request_id) is MESSAGES
    assert output_language.for_user_text(MESSAGES) is MESSAGES


@pytest.mark.parametrize("request_id", INTERNAL)
def test_english_leaves_internal_calls_alone(request_id):
    english()
    assert output_language.with_directive(MESSAGES, request_id) is MESSAGES


def test_without_a_user_message_the_directive_goes_last_and_the_type_is_kept():
    english()
    system_only = ({"role": "system", "content": "persona"},)
    assert contents(output_language.for_user_text(system_only)) == ["persona", DIRECTIVE]
    assert isinstance(output_language.for_user_text(system_only), tuple)
    assert isinstance(output_language.for_user_text(list(system_only)), list)


def test_the_directive_asks_for_english_and_keeps_the_formats():
    assert "natural, fluent English" in DIRECTIVE
    assert "Chinese material in this prompt is reference only" in DIRECTIVE
    assert "in the language the user wrote them" in DIRECTIVE
    assert "JSON keys, enum values, tags, markers and ids stay exactly as specified" in DIRECTIVE
    assert len(DIRECTIVE) < 1000  # a short note, not a second prompt


def test_lengths_are_scaled_only_in_english():
    assert output_language.scaled(1200) == 1200
    english()
    assert output_language.scaled(1200) == 1200 * output_language.LENGTH_FACTOR


# --- the request the provider receives ------------------------------------------------------------------------------

async def exchange(call, *, stream_reply=False):
    """Run ``call(adapter)`` against a local server; returns the body and headers the server saw."""
    seen = []

    async def handler(request):
        seen.append({"body": await request.json(), "headers": {k: v for k, v in request.headers.items() if k != "Host"}})
        if stream_reply:
            chunk = {"choices": [{"delta": {"content": "hello"}, "finish_reason": "stop"}]}
            return web.Response(text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n", content_type="text/event-stream")
        return web.json_response({"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    async with TestClient(TestServer(app)) as client:
        adapter = OpenAICompatibleAdapter(make_config(str(client.make_url("/v1")), model="synthetic-model"))
        await call(adapter)
    assert len(seen) == 1
    return seen[0]


def sent_to_provider(call, *, streams):
    return {request_id: asyncio.run(exchange(lambda adapter: call(adapter, request_id), stream_reply=streams))
            for request_id in ("letter-reply:3f2a", "personal-chat:ex1:1", "diary:2026-10-07", "life:7c1a")}


def gateway_calls():
    """Every way the gateway is asked for text: (name, async call(adapter, request_id), is a stream)."""
    json_object = {"type": "json_object"}

    async def complete(adapter, request_id):
        await adapter.complete(MESSAGES, request_id=request_id)

    async def complete_scoped(adapter, request_id):
        await adapter.complete_scoped(MESSAGES, request_id=request_id, scope=GatewayRequestScope.TEXT_LETTER_MAX_REASONING)

    async def structured_json(adapter, request_id):
        await adapter.complete_structured_scoped(
            MESSAGES, request_id=request_id, scope=GatewayRequestScope.PERSONAL_CHAT_JSON, response_format=json_object)

    async def structured_background(adapter, request_id):
        await adapter.complete_structured_scoped(
            MESSAGES, request_id=request_id, scope=GatewayRequestScope.BACKGROUND_REASONING, response_format=json_object)

    async def stream(adapter, request_id):
        _ = [delta async for delta in adapter.stream(MESSAGES, request_id=request_id)]

    async def stream_scoped(adapter, request_id):
        _ = [delta async for delta in adapter.stream_scoped(
            MESSAGES, request_id=request_id, scope=GatewayRequestScope.PERSONAL_CHAT_JSON)]

    return [("complete", complete, False), ("complete_scoped", complete_scoped, False),
            ("structured_json", structured_json, False), ("structured_background", structured_background, False),
            ("stream", stream, True), ("stream_scoped", stream_scoped, True)]


@pytest.mark.parametrize("name,call,streams", gateway_calls(), ids=[item[0] for item in gateway_calls()])
def test_chinese_requests_are_byte_identical_to_the_code_without_the_hook(monkeypatch, name, call, streams):
    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    with_hook = sent_to_provider(call, streams=streams)
    monkeypatch.setattr(output_language, "with_directive", lambda messages, *args, **kwargs: messages)
    original = sent_to_provider(call, streams=streams)
    assert with_hook == original
    assert all(DIRECTIVE not in json.dumps(sent["body"], ensure_ascii=False) for sent in with_hook.values())
    assert all(ui_language.HEADER not in sent["headers"] for sent in with_hook.values())


@pytest.mark.parametrize("name,call,streams", gateway_calls(), ids=[item[0] for item in gateway_calls()])
def test_english_requests_gain_exactly_the_directive_for_user_facing_calls(monkeypatch, name, call, streams):
    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    chinese = sent_to_provider(call, streams=streams)
    english()
    for request_id, after in sent_to_provider(call, streams=streams).items():
        before = chinese[request_id]
        sent = [m["content"] for m in after["body"]["messages"]]
        expected = [m["content"] for m in before["body"]["messages"]]
        if output_language.writes_for_user(request_id):
            assert sent.count(DIRECTIVE) == 1
            index = sent.index(DIRECTIVE)
            assert sent[:index] + sent[index + 1:] == expected
            assert after["body"]["messages"][index]["role"] == "system"
            assert sent[index + 1] == "now"  # right in front of what the user just said
        else:
            assert sent == expected
        # nothing but the messages differs
        assert {k: v for k, v in after["body"].items() if k != "messages"} == \
               {k: v for k, v in before["body"].items() if k != "messages"}
        assert after["headers"].pop(ui_language.HEADER) == "en"
        assert after["headers"] == before["headers"] | {"Content-Length": after["headers"]["Content-Length"]}


def test_the_body_without_a_request_id_is_the_reference_for_chinese(monkeypatch):
    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    adapter = OpenAICompatibleAdapter(make_config("http://127.0.0.1:1/v1"))
    scope = GatewayRequestScope.TEXT_LETTER_MAX_REASONING
    reference = adapter._body(MESSAGES, stream=False, scope=scope)
    assert DIRECTIVE not in json.dumps(reference)
    for request_id in USER_FACING:
        assert adapter._body(MESSAGES, stream=False, scope=scope, request_id=request_id) == reference
    english()
    assert adapter._body(MESSAGES, stream=False, scope=scope, request_id="life:7c1a") == reference
    changed = adapter._body(MESSAGES, stream=False, scope=scope, request_id="letter-reply:3f2a")
    assert changed != reference and DIRECTIVE in json.dumps(changed)


def test_the_directive_does_not_use_up_the_input_room_the_caller_measured(monkeypatch):
    """The input check runs before the directive is added, so a prompt that fits keeps fitting."""
    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    english()
    adapter = OpenAICompatibleAdapter(make_config("http://127.0.0.1:1/v1", max_input_chars=20))
    messages = ({"role": "system", "content": "x" * 10}, {"role": "user", "content": "y" * 10})
    body = adapter._body(messages, stream=False, request_id="letter-reply:1")
    assert DIRECTIVE in [m["content"] for m in body["messages"]]


def test_the_responses_api_style_gets_the_directive_too(monkeypatch):
    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    adapter = OpenAICompatibleAdapter(make_config("http://127.0.0.1:1/v1", api_style="responses"))
    reference = adapter._body(MESSAGES, stream=False)
    assert adapter._body(MESSAGES, stream=False, request_id="diary:2026-10-07") == reference
    english()
    body = adapter._body(MESSAGES, stream=False, request_id="diary:2026-10-07")
    assert [m["content"] for m in body["input"]] == ["persona", "earlier", "earlier reply", DIRECTIVE, "now"]


def test_english_answers_may_be_longer_for_user_facing_calls_only(monkeypatch):
    monkeypatch.setenv("B03_TEST_KEY", "TEST")
    adapter = OpenAICompatibleAdapter(make_config("http://127.0.0.1:1/v1", max_output_chars=100))
    assert adapter._output_limit("diary:2026-10-07", None) == 100
    english()
    assert adapter._output_limit("diary:2026-10-07", None) == 100 * output_language.LENGTH_FACTOR
    assert adapter._output_limit("life:7c1a", GatewayRequestScope.BACKGROUND_REASONING) == 100


# --- the quality rewrite shares its request id with the reviewer, so it asks for English itself ----------------------

def test_the_rewrite_asks_for_english_and_nothing_else_moves(monkeypatch):
    import runtime.reply.reply_model_quality as quality
    from tests.persona.test_reply_model_quality import ROOT, _context

    captured = []
    monkeypatch.setattr(quality, "_complete_text", lambda gateway, messages, *a, **k: captured.append(messages) or "Rewritten.")
    rewriter = quality.GatewayPersonaRewriter(SimpleNamespace(), ROOT / "missing.json", 2)
    generation = ({"role": "system", "content": "persona"}, {"role": "user", "content": "hello"})
    context = _context()  # built once, so the payload of both runs is the same
    assert rewriter.rewrite_with_messages("candidate", context, (), generation) == "Rewritten."
    assert all(DIRECTIVE not in m["content"] for m in captured[0])

    english()
    rewriter.rewrite_with_messages("candidate", context, (), generation)
    sent = captured[1]
    assert contents(sent).count(DIRECTIVE) == 1
    index = contents(sent).index(DIRECTIVE)
    assert [m for i, m in enumerate(sent) if i != index] == list(captured[0])
    assert sent[-1]["role"] == "user"  # the payload is still last


# --- the writer as the reply pipeline drives it ---------------------------------------------------------------------

@pytest.mark.parametrize("request_id,scope", (
    ("letter-reply:3f2a", GatewayRequestScope.TEXT_LETTER_MAX_REASONING),
    ("personal-chat:ex1:1", GatewayRequestScope.PERSONAL_CHAT_JSON),
))
@pytest.mark.parametrize("streams", (False, True))
def test_the_orchestrated_writer_reaches_the_provider_with_the_directive_in_english(monkeypatch, request_id, scope, streams):
    from reply_orchestrator import ReplyOrchestrator, ReplyRequest

    monkeypatch.setenv("B03_TEST_KEY", "TEST")

    async def write(adapter):
        adapter.stream_enabled = streams
        result = await ReplyOrchestrator(adapter).run(ReplyRequest(
            messages=MESSAGES, request_id=request_id, gateway_scope=scope, max_input_chars=1000))
        assert result.completed

    zh = asyncio.run(exchange(write, stream_reply=streams))
    english()
    en = asyncio.run(exchange(write, stream_reply=streams))
    assert DIRECTIVE not in json.dumps(zh["body"])
    assert contents(en["body"]["messages"]) == ["persona", "earlier", "earlier reply", DIRECTIVE, "now"]
    assert {k: v for k, v in en["body"].items() if k != "messages"} == {k: v for k, v in zh["body"].items() if k != "messages"}
