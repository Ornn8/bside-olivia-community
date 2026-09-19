import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer


class Interpreter:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def interpret(self, text):
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return {"acts": [{"quote": text, "kind": "invitation", "meaning": "用户邀请以后聊音乐。"}]}


class Orchestrator:
    def __init__(self):
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        return ReplyResult(request.request_id, ReplyState.COMPLETED, text="下次可以聊聊音乐。")


def context(mode=ReplyMode.TEXT_LETTER):
    return ReplyContext.create(mode, trusted_time=TrustedTime(datetime(2026, 9, 7, tzinfo=timezone.utc)))


def request():
    return ReplyRequest(content="以后一起聊音乐吧。", messages=[
        {"role": "system", "content": "人格及事实规则"},
        {"role": "user", "content": "以后一起聊音乐吧。"},
    ])


def test_interpretation_precedes_generation_and_preserves_original_request():
    original = request()
    interpreter, orchestrator = Interpreter(), Orchestrator()
    pipeline = ReplyPipeline(orchestrator, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                             discover_runtime_ports=False, current_turn_interpreter=interpreter)
    result = asyncio.run(pipeline.run(original, context()))
    assert result.state is ReplyState.COMPLETED
    assert result.text == "下次可以聊聊音乐。"
    assert interpreter.calls == [original.content]
    projected = orchestrator.requests[0]
    assert projected.messages[-1] == original.messages[-1]
    assert projected.messages[0]["content"].startswith("人格及事实规则")
    assert "current_turn_interpretation" in projected.messages[0]["content"]
    assert original.messages[0]["content"] == "人格及事实规则"
    assert "acts" not in repr(result)


def test_assembled_persona_request_may_clear_content(monkeypatch):
    import runtime.reply.reply_pipeline as module
    original = request()
    monkeypatch.setattr(module, "_prepare_generation_request", lambda *args: module._PreparedGeneration(
        replace(original, content=None),
    ))
    interpreter, orchestrator = Interpreter(), Orchestrator()
    pipeline = ReplyPipeline(orchestrator, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                             discover_runtime_ports=False, current_turn_interpreter=interpreter)
    result = asyncio.run(pipeline.run(original, context()))
    assert result.state is ReplyState.COMPLETED
    assert interpreter.calls == [original.content]
    assert orchestrator.requests[0].messages[-1]["content"] == original.content


@pytest.mark.parametrize("error", [RuntimeError("PRIVATE PROVIDER DETAIL"), TimeoutError()])
def test_failed_interpretation_cannot_generate_or_expose_a_candidate(error):
    interpreter, orchestrator = Interpreter(error), Orchestrator()
    pipeline = ReplyPipeline(orchestrator, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                             discover_runtime_ports=False, current_turn_interpreter=interpreter)
    result = asyncio.run(pipeline.run(request(), context()))
    assert result.state is ReplyState.FAILED
    assert result.error_code == "CURRENT_TURN_INTERPRETATION_FAILED"
    assert result.text == "" and not orchestrator.requests
    assert "PRIVATE" not in repr(result)


def test_video_bypasses_optional_letter_interpretation():
    interpreter, orchestrator = Interpreter(), Orchestrator()
    pipeline = ReplyPipeline(orchestrator, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                             discover_runtime_ports=False, current_turn_interpreter=interpreter)
    asyncio.run(pipeline.run(request(), context(ReplyMode.SPOKEN_VIDEO)))
    assert interpreter.calls == []
    assert len(orchestrator.requests) == 1


def test_interpretation_cancellation_propagates_without_generation():
    interpreter, orchestrator = Interpreter(asyncio.CancelledError()), Orchestrator()
    pipeline = ReplyPipeline(orchestrator, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                             discover_runtime_ports=False, current_turn_interpreter=interpreter)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pipeline.run(request(), context()))
    assert not orchestrator.requests
