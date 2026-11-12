import asyncio

import pytest

from runtime import media_capabilities as mc


ENV = {'OLIVIA_GPU_API_URL': 'https://api.example.cn', 'OLIVIA_GPU_API_KEY': 'olivia-test-key'}


@pytest.fixture
def clock(monkeypatch):
    now = {'t': 1000.0}
    monkeypatch.setattr(mc.time, 'monotonic', lambda: now['t'])
    monkeypatch.delenv('OLIVIA_LOCAL_DATA_ROOT', raising=False)
    mc._state.clear()
    yield now
    mc._state.clear()


def fetcher(monkeypatch, answers):
    calls = []

    async def fetch(url, token, timeout=3):
        calls.append(url)
        answer = answers.pop(0) if answers else None
        return answer

    monkeypatch.setattr(mc, '_fetch', fetch)
    return calls


def test_a_good_answer_survives_failed_refreshes_for_hours(clock, monkeypatch):
    calls = fetcher(monkeypatch, [{'speech_experience': True}])

    async def run():
        assert (await mc.current(ENV))['speech_experience'] is True
        clock['t'] += 30
        assert (await mc.current(ENV))['speech_experience'] is True and len(calls) == 1
        clock['t'] += 60  # stale: answered at once, refreshed in the background, which fails
        assert (await mc.current(ENV))['speech_experience'] is True
        await asyncio.sleep(0)
        assert len(calls) == 2 and mc._state[next(iter(mc._state))].get('failed_at') == clock['t']
        clock['t'] += mc.STALE_SECONDS - 200
        assert (await mc.current(ENV))['speech_experience'] is True
        clock['t'] += 400  # past the keeping time without a good answer
        assert await mc.current(ENV) is None
    asyncio.run(run())


def test_a_restarted_client_starts_from_the_last_good_answer(clock, monkeypatch, tmp_path):
    env = {**ENV, 'OLIVIA_LOCAL_DATA_ROOT': str(tmp_path)}
    calls = fetcher(monkeypatch, [{'kinds': ['cover']}, None, None])

    async def run():
        assert (await mc.current(env))['kinds'] == ['cover']       # read and kept in the file
        assert (tmp_path / mc.KEPT_FILE).is_file()
        mc._state.clear()                                          # the client was restarted; the service is slow now
        real = mc.time.time
        monkeypatch.setattr(mc.time, 'time', lambda: real() + 300)  # five minutes later by the wall clock
        clock['t'] += 300
        assert (await mc.current(env))['kinds'] == ['cover']       # answered at once from the file
        await asyncio.sleep(0)
        assert len(calls) == 2                                     # and refreshed in the background (which failed)
        mc._state.clear()
        assert await mc.current(ENV) is None                       # no data folder: nothing kept, nothing to start from
    asyncio.run(run())


def test_the_kept_answer_is_not_used_for_another_account_or_when_too_old(clock, monkeypatch, tmp_path):
    env = {**ENV, 'OLIVIA_LOCAL_DATA_ROOT': str(tmp_path)}
    fetcher(monkeypatch, [{'kinds': ['cover']}, None, None])

    async def run():
        assert await mc.current(env)
        mc._state.clear()
        assert await mc.current({**env, 'OLIVIA_GPU_API_KEY': 'olivia-other-key'}) is None
        mc._state.clear()
        saved = (tmp_path / mc.KEPT_FILE).read_text(encoding='utf-8')
        real = mc.time.time
        monkeypatch.setattr(mc.time, 'time', lambda: real() + mc.STALE_SECONDS + 10)
        assert await mc.current(env) is None                        # a day-old answer is not trusted
        (tmp_path / mc.KEPT_FILE).write_text('{"broken": ', encoding='utf-8')
        mc._state.clear()
        assert await mc.current(env) is None                        # an unreadable file is simply no file
        assert saved
    asyncio.run(run())


def test_a_cover_waiting_for_the_answer_gets_a_long_retry():
    assert mc.RETRY_TIMEOUT_SECONDS >= 20 and mc.TIMEOUT_SECONDS == 3


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


def test_a_retry_reads_again_at_once_with_a_longer_wait_even_right_after_a_failure(clock, monkeypatch):
    calls = fetcher(monkeypatch, [None, {'kinds': ['cover']}])

    async def run():
        assert await mc.current(ENV) is None
        assert await mc.current(ENV) is None and len(calls) == 1      # an ordinary caller is not held again
        assert (await mc.current(ENV, retry=True))['kinds'] == ['cover'] and len(calls) == 2
    asyncio.run(run())
