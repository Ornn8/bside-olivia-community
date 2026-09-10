import asyncio
from types import SimpleNamespace

def test_route_cache_reuses_only_same_successful_context(monkeypatch):
    import local_server as server
    from letter_triage import TriageResult
    calls = []
    outcome = {'status': 'completed'}
    async def classify(content):
        calls.append(content)
        return TriageResult('unknown', 'text_letter', 'synthetic', outcome['status'], True)
    router = SimpleNamespace(classify=classify, gateway=object(), routing_context=None, environ={})
    ready = {'voice_reply': True, 'singing_video': True}
    videos = {'voice_reply': False}
    clock = [10.0]
    monkeypatch.setattr(server, 'emotion_triage', router)
    monkeypatch.setattr(server, '_route_readiness', lambda: dict(ready))
    monkeypatch.setattr(server, 'video_reply_settings_store', SimpleNamespace(videos_snapshot=lambda: dict(videos)))
    monkeypatch.setattr(server, '_route_decision_cache', {})
    monkeypatch.setattr(server.time, 'monotonic', lambda: clock[0])
    async def exercise():
        routes = {'voice_reply': True}
        first = await server._classify_managed_route('letter', routes)
        cached = await server._classify_managed_route('letter', routes)
        assert cached.reply_mode == first.reply_mode and not cached.llm_called
        assert len(calls) == 1
        await server._classify_managed_route('different', routes)
        ready['voice_reply'] = False
        await server._classify_managed_route('letter', routes)
        videos['voice_reply'] = True
        await server._classify_managed_route('letter', routes)
        router.gateway = object()
        await server._classify_managed_route('letter', routes)
        clock[0] += 301
        await server._classify_managed_route('letter', routes)
        assert len(calls) == 6
        outcome['status'] = 'unavailable'
        await server._classify_managed_route('failure', routes)
        await server._classify_managed_route('failure', routes)
        assert len(calls) == 8
        outcome['status'] = 'completed'
        for index in range(130):
            await server._classify_managed_route(f'bounded-{index}', routes)
        assert len(server._route_decision_cache) == 128
    asyncio.run(exercise())
    assert all(len(key) == 64 for key in server._route_decision_cache)
