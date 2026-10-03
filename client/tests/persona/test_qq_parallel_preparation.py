import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
from runtime.personal_chat.presentation import CURRENT
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer


NOW = datetime(2026, 10, 3, 9, tzinfo=timezone.utc)


def pipeline(world, emotion):
    class Engine:
        gateway = SimpleNamespace(adapter=SimpleNamespace(
            daily_life=object(), prepare_daily_life_fragments=world,
            prepare_character_emotion=emotion))
        calls = 0

        async def run(self, request):
            self.calls += 1
            return ReplyResult('parallel', ReplyState.COMPLETED, text='我们慢慢聊。')

    return ReplyPipeline(Engine(), reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                         discover_runtime_ports=False)


def context():
    return ReplyContext.create(ReplyMode.FUTURE_IM, trusted_time=TrustedTime(NOW),
                               future_im_enabled=True)


def test_qq_world_and_emotion_prepare_together_with_same_input_and_time():
    async def scenario():
        started = [asyncio.Event(), asyncio.Event()]
        seen = []
        async def world(text, *, now):
            seen.append(('world', text, now))
            started[0].set()
            await started[1].wait()
            return ()
        async def emotion(text, *, now):
            seen.append(('emotion', text, now))
            started[1].set()
            await started[0].wait()
        worker = pipeline(world, emotion)
        token = CURRENT.set({'raw_user_text': '今晚不赶时间。'})
        try:
            result = await asyncio.wait_for(worker.run(
                ReplyRequest(content='今晚不赶时间。'), context()), .5)
        finally:
            CURRENT.reset(token)
        assert result.state is ReplyState.COMPLETED and worker.orchestrator.calls == 1
        assert seen == [('world', '今晚不赶时间。', NOW), ('emotion', '今晚不赶时间。', NOW)]
        assert set(result.stage_timing_seconds) >= {'world', 'emotion', 'writer', 'total'}
        assert all(type(t) is float and 0 <= t < 1 for t in result.stage_timing_seconds.values())
    asyncio.run(scenario())


def test_cancelled_qq_cleans_both_preparation_children_without_writer():
    async def scenario():
        started = [asyncio.Event(), asyncio.Event()]
        stopped = []
        async def branch(index):
            started[index].set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(index)
        async def world(text, *, now):
            return await branch(0)
        async def emotion(text, *, now):
            return await branch(1)
        worker = pipeline(world, emotion)
        task = asyncio.create_task(worker.run(ReplyRequest(content='等等。'), context()))
        await asyncio.wait_for(asyncio.gather(*(s.wait() for s in started)), .5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sorted(stopped) == [0, 1] and worker.orchestrator.calls == 0
    asyncio.run(scenario())


def test_world_error_cancels_optional_preparation_and_never_generates():
    from runtime.reply.world_context_selection import WorldSelectionError
    async def scenario():
        started = asyncio.Event()
        stopped = asyncio.Event()
        async def world(text, *, now):
            await started.wait()
            raise WorldSelectionError('JEV_WORLD_SELECTION_FAILED')
        async def emotion(text, *, now):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        worker = pipeline(world, emotion)
        result = await asyncio.wait_for(worker.run(ReplyRequest(content='今晚休息。'), context()), .5)
        assert result.state is ReplyState.FAILED and result.error_code == 'JEV_WORLD_SELECTION_FAILED'
        assert stopped.is_set() and worker.orchestrator.calls == 0
        assert set(result.stage_timing_seconds) >= {'world', 'emotion', 'total'}
    asyncio.run(scenario())


def test_world_addressing_is_frozen_before_emotion_can_update_store(monkeypatch):
    import local_server
    from runtime.reply import world_context_selection
    async def scenario():
        changed = asyncio.Event()
        state = {'calls': ['原来的称呼']}
        class Store:
            def reply_candidates(self, *, now):
                return {'rhythm': {}}
            def addressing_profile(self, *, now):
                return list(state['calls'])
        async def select(*args):
            await changed.wait()
            return '{}'
        monkeypatch.setattr(world_context_selection, 'select_world_context', select)
        adapter = object.__new__(local_server.LetterAdapter)
        adapter.daily_life = SimpleNamespace(store=Store())
        adapter.recent_letter_fragments = lambda content, now: ()
        task = asyncio.create_task(adapter.prepare_daily_life_fragments('晚上好。', now=NOW))
        await asyncio.sleep(0)
        state['calls'] = ['晚到的新称呼']
        changed.set()
        fragments = await task
        assert '原来的称呼' in fragments[-1].text and '晚到的新称呼' not in fragments[-1].text
    asyncio.run(scenario())
