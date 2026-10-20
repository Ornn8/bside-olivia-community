import asyncio

import pytest

from runtime import media_capabilities as mc


ENV = {'OLIVIA_GPU_API_URL': 'https://api.example.cn', 'OLIVIA_GPU_API_KEY': 'olivia-test-key'}


@pytest.fixture
def clock(monkeypatch):
    now = {'t': 1000.0}
    monkeypatch.setattr(mc.time, 'monotonic', lambda: now['t'])
    mc._state.clear()
    yield now
    mc._state.clear()


def fetcher(monkeypatch, answers):
    calls = []

    async def fetch(url, token):
        calls.append(url)
        answer = answers.pop(0) if answers else None
        return answer

    monkeypatch.setattr(mc, '_fetch', fetch)
    return calls


def test_a_good_answer_survives_failed_refreshes_for_thirty_minutes(clock, monkeypatch):
    calls = fetcher(monkeypatch, [{'speech_experience': True}])

    async def run():
        assert (await mc.current(ENV))['speech_experience'] is True
        clock['t'] += 30
        assert (await mc.current(ENV))['speech_experience'] is True and len(calls) == 1
        clock['t'] += 60  # stale: answered at once, refreshed in the background, which fails
        assert (await mc.current(ENV))['speech_experience'] is True
        await asyncio.sleep(0)
        assert len(calls) == 2 and mc._state[next(iter(mc._state))].get('failed_at') == clock['t']
        clock['t'] += 1700
        assert (await mc.current(ENV))['speech_experience'] is True
        clock['t'] += 200  # past thirty minutes without a good answer
        assert await mc.current(ENV) is None
    asyncio.run(run())


def test_a_cold_failure_does_not_hold_every_reply(clock, monkeypatch):
    calls = fetcher(monkeypatch, [None, {'speech_experience': True}])

    async def run():
        assert await mc.current(ENV) is None and len(calls) == 1
        clock['t'] += 10
        assert await mc.current(ENV) is None and len(calls) == 1
        clock['t'] += 60
        assert (await mc.current(ENV))['speech_experience'] is True and len(calls) == 2
    asyncio.run(run())


def test_another_key_never_reuses_an_answer(clock, monkeypatch):
    calls = fetcher(monkeypatch, [{'speech_experience': True}, None])

    async def run():
        assert await mc.current(ENV)
        assert await mc.current({**ENV, 'OLIVIA_GPU_API_KEY': 'olivia-other-key'}) is None
        assert len(calls) == 2
        assert await mc.current({}) is None
    asyncio.run(run())


def test_speech_and_piano_share_one_read(clock, monkeypatch):
    from runtime import piano
    from runtime.personal_chat import speech
    piano_caps = {'enabled': True, 'max_seconds': 120, 'max_midi_bytes': 524288, 'original': True,
                  'price_ranges_cents': {'midi': [60, 80], 'original': [150, 200]}}
    calls = fetcher(monkeypatch, [{'speech_experience': True, 'piano': piano_caps}])

    async def run():
        assert await speech.supported(ENV) is True
        assert (await piano.capabilities(ENV))['enabled'] is True
        assert len(calls) == 1
    asyncio.run(run())
