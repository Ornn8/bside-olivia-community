"""The real pipeline resumes provider failures, never a rejected candidate."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading

import pytest

from runtime.personal_chat.presentation import CURRENT
from runtime.reply.reply_context import IntimacyRequest, ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
from runtime.reply.reply_pipeline import ReplyPipeline
from runtime.reply.reply_reviewer import (
    NullReviewer, ReviewResult, ReviewStatus, ReviewVerdict, ReviewerScores, ReviewerViolation,
)
from tests.http.test_personal_chat_decision import envelope
from tests.persona.test_stage_recovery import (
    FactReviewer, FactRewriter, ORIGINAL, REPAIRED, _pass,
)


GOOD = 'Let us talk about your present plans instead.'
NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


class Engine:
    def __init__(self, bodies):
        self.bodies = bodies
        self.calls = 0

    async def run(self, request):
        value = self.bodies[min(self.calls, len(self.bodies) - 1)]
        self.calls += 1
        return ReplyResult(request.request_id, ReplyState.COMPLETED, text=value)


def setup(monkeypatch, bodies, *, reviewer=None, rewriter=None):
    # Every provider is a local fixture; never discover a configured sidecar.
    monkeypatch.delenv('OLIVIA_JEV_DECISION_URL', raising=False)
    engine = Engine(bodies)
    reviewer = reviewer or FactReviewer()
    rewriter = rewriter or FactRewriter()
    pipeline = ReplyPipeline(engine, reviewer=reviewer, rewriter=rewriter,
                             discover_runtime_ports=False)
    context = ReplyContext.create(ReplyMode.FUTURE_IM, future_im_enabled=True,
                                 trusted_time=TrustedTime(NOW))
    metadata = dict(structured=True, channel='qq', raw_user_text='I never mentioned climbing Taishan.',
        received_source_id='synthetic-received', input_revision=0, turn_is_current=lambda: True,
        decision_now=NOW.isoformat())
    return pipeline, engine, reviewer, rewriter, context, metadata


async def attempt(pipeline, context, metadata, number):
    metadata['generation_attempts'] = number
    token = CURRENT.set(metadata)
    try:
        return await pipeline.run(ReplyRequest(request_id=f'synthetic-attempt-{number}',
            messages=({'role': 'user', 'content': metadata['raw_user_text']},)), context)
    finally:
        CURRENT.reset(token)


def test_malformed_first_candidate_gets_fresh_writer_on_second_attempt(monkeypatch):
    pipeline, engine, _, _, context, metadata = setup(monkeypatch,
        ['not JSON', envelope(text=GOOD, delivery='text')])
    async def scenario():
        first = await attempt(pipeline, context, metadata, 1)
        second = await attempt(pipeline, context, metadata, 2)
        assert first.state is ReplyState.FAILED and first.error_code == 'PERSONAL_CHAT_DECISION_INVALID'
        assert second.state is ReplyState.COMPLETED and GOOD in second.text
        assert engine.calls == 2 and not pipeline._stage_recoveries
    asyncio.run(scenario())


def test_non_chat_child_cancellation_does_not_detach_emotion_branch(monkeypatch):
    monkeypatch.delenv('OLIVIA_JEV_DECISION_URL', raising=False)
    async def scenario():
        from types import SimpleNamespace
        emotion_started, interpreted, emotion_stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def emotion(text, *, now):
            emotion_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                emotion_stopped.set()
        class Interpreter:
            async def interpret(self, text):
                await emotion_started.wait()
                interpreted.set()
                raise asyncio.CancelledError()
        engine = Engine([])
        engine.gateway = SimpleNamespace(adapter=SimpleNamespace(prepare_character_emotion=emotion))
        pipeline = ReplyPipeline(engine, reviewer=NullReviewer(), rewriter=FactRewriter(),
            discover_runtime_ports=False, current_turn_interpreter=Interpreter())
        context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=TrustedTime(NOW))
        task = asyncio.create_task(pipeline.run(ReplyRequest(content='hello',
            messages=({'role': 'user', 'content': 'hello'},)), context))
        await asyncio.wait_for(interpreted.wait(), .5)
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert emotion_stopped.is_set() and engine.calls == 0
    asyncio.run(scenario())


class FinalHardReviewer(FactReviewer):
    def review_with_messages(self, candidate, context, messages, **kwargs):
        if candidate == REPAIRED:
            self.calls += 1
            return ReviewResult(ReviewStatus.COMPLETED, ReviewVerdict.BLOCK,
                (ReviewerViolation('MEMORY_FABRICATION', 'hard', 0, len(candidate)),),
                ReviewerScores(100, 0, 100, 100), _pass().intimacy_request, ())
        return super().review_with_messages(candidate, context, messages, **kwargs)


def test_final_hard_block_drops_bad_rewrite_before_fresh_writer(monkeypatch):
    pipeline, engine, _, rewriter, context, metadata = setup(monkeypatch,
        [envelope(text=ORIGINAL, delivery='text'), envelope(text=GOOD, delivery='text')],
        reviewer=FinalHardReviewer())
    async def scenario():
        first = await attempt(pipeline, context, metadata, 1)
        assert first.state is ReplyState.FAILED and first.quality_status == 'blocked'
        second = await attempt(pipeline, context, metadata, 2)
        assert second.state is ReplyState.COMPLETED and GOOD in second.text
        assert engine.calls == 2 and rewriter.calls == 1
    asyncio.run(scenario())


def test_unrepaired_hard_facts_stay_blocked_through_both_attempts(monkeypatch):
    pipeline, engine, _, rewriter, context, metadata = setup(monkeypatch,
        [envelope(text=ORIGINAL, delivery='text')], reviewer=FinalHardReviewer())
    async def scenario():
        results = [await attempt(pipeline, context, metadata, number) for number in (1, 2)]
        assert all(result.state is ReplyState.FAILED and result.quality_status == 'blocked'
                   and not result.text for result in results)
        assert engine.calls == rewriter.calls == 2 and not pipeline._stage_recoveries
    asyncio.run(scenario())


@pytest.mark.parametrize('failed_stage, expected_calls', [
    ('rewrite', {'writer': 1, 'reviewer': 2, 'rewriter': 2}),
    ('final_review', {'writer': 1, 'reviewer': 3, 'rewriter': 1}),
])
def test_transient_failure_resumes_successful_prefix_only(monkeypatch, failed_stage, expected_calls):
    pipeline, engine, reviewer, rewriter, context, metadata = setup(monkeypatch,
        [envelope(text=ORIGINAL, delivery='text')],
        reviewer=FactReviewer(fail_final=failed_stage == 'final_review'),
        rewriter=FactRewriter(fail_first=failed_stage == 'rewrite'))
    async def scenario():
        first = await attempt(pipeline, context, metadata, 1)
        second = await attempt(pipeline, context, metadata, 2)
        assert first.state is ReplyState.FAILED and first.quality_status == 'blocked'
        assert second.state is ReplyState.COMPLETED and REPAIRED in second.text
        assert second.stage_actual_calls == expected_calls
        assert engine.calls == 1
        assert reviewer.calls == expected_calls['reviewer']
        assert rewriter.calls == expected_calls['rewriter']
        assert not pipeline._stage_recoveries
    asyncio.run(scenario())


def test_adjudicated_request_context_keeps_confirmed_evidence_on_transient_retry(monkeypatch):
    class RequestedReviewer(FactReviewer):
        def review_with_messages(self, *args, **kwargs):
            return replace(super().review_with_messages(*args, **kwargs),
                           intimacy_request=IntimacyRequest.REQUESTED)
    pipeline, engine, reviewer, rewriter, context, metadata = setup(monkeypatch,
        [envelope(text=ORIGINAL, delivery='text')],
        reviewer=RequestedReviewer(), rewriter=FactRewriter(fail_first=True))
    async def scenario():
        first = await attempt(pipeline, context, metadata, 1)
        second = await attempt(pipeline, context, metadata, 2)
        assert first.error_code == 'REWRITE_PROVIDER_UNAVAILABLE'
        assert second.state is ReplyState.COMPLETED and REPAIRED in second.text
        assert engine.calls == 1 and reviewer.evidence_calls == 1 and rewriter.calls == 2
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['context', 'revision', 'original'])
def test_changed_input_cannot_adopt_cached_writer(monkeypatch, change):
    pipeline, engine, _, _, context, metadata = setup(monkeypatch,
        [envelope(text=ORIGINAL, delivery='text'), envelope(text=GOOD, delivery='text')],
        rewriter=FactRewriter(fail_first=True))
    async def scenario():
        first = await attempt(pipeline, context, metadata, 1)
        assert first.state is ReplyState.FAILED
        second_context = context
        if change == 'context':
            second_context = replace(context, trusted_time=TrustedTime(NOW + timedelta(seconds=1)))
        elif change == 'revision':
            metadata['input_revision'] = 1
        else:
            metadata['raw_user_text'] = 'A revised user original changes this turn.'
        second = await attempt(pipeline, second_context, metadata, 2)
        assert second.state is ReplyState.COMPLETED and GOOD in second.text
        assert engine.calls == 2 and not pipeline._stage_recoveries
    asyncio.run(scenario())


def test_cancelled_review_cannot_start_a_new_paid_stage_or_refill_cache(monkeypatch):
    started, release, returned = threading.Event(), threading.Event(), threading.Event()
    class HeldReviewer(FactReviewer):
        def review_with_messages(self, candidate, context, messages, **kwargs):
            if candidate == ORIGINAL:
                started.set()
                assert release.wait(2)
                try:
                    return super().review_with_messages(candidate, context, messages, **kwargs)
                finally:
                    returned.set()
            return super().review_with_messages(candidate, context, messages, **kwargs)
    pipeline, engine, _, rewriter, context, metadata = setup(monkeypatch,
        [envelope(text=ORIGINAL, delivery='text'), envelope(text=GOOD, delivery='text')],
        reviewer=HeldReviewer())
    async def scenario():
        first = asyncio.create_task(attempt(pipeline, context, metadata, 1))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not pipeline._stage_recoveries
            second = await attempt(pipeline, context, metadata, 2)
            assert second.state is ReplyState.COMPLETED and GOOD in second.text
            release.set()
            assert await asyncio.to_thread(returned.wait, 1)
            await asyncio.sleep(.02)
            assert engine.calls == 2 and rewriter.calls == 0
            assert not pipeline._stage_recoveries
        finally:
            release.set()
    asyncio.run(scenario())
