import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from original_client_server import create_original_client_server_runtime
from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime
from runtime.private_world.port import PrivateWorldSnapshot


def test_visible_life_endpoint_matches_persisted_reply_context(tmp_path):
    async def run():
        store = DailyLifeStore(tmp_path / "life.sqlite3")
        now = datetime.now(timezone.utc)
        store.publish_day("day:test", {"location": "琴房", "activity": "练琴", "note": "今天想把这段弹稳。"}, [], occurred_at=now)
        life = DailyLifeRuntime(store, lambda: None, lambda: "")
        async def fallback(request):
            return web.Response(status=404)
        runtime = create_original_client_server_runtime(fallback, daily_life=life, trusted_origins=("https://client.example",))
        async with TestClient(TestServer(runtime.app)) as client:
            response = await client.get("/toy/companion/private-world/life", headers={"Origin": "https://client.example"})
            assert response.status == 200
            data = await response.json()
            assert data["current"]["note"] == "今天想把这段弹稳。"
            assert data["current"]["note"] in store.reply_context("今天怎么样？", now=now)
            assert "levels" not in data and "trust" not in data
            history = await client.get("/toy/companion/private-world/life?history=1", headers={"Origin": "https://client.example"})
            archived = await history.json()
            assert archived["schema_version"] == "olivia.daily-life.history.v1"
            assert archived["moments"][0]["id"] == "day:test"
            assert archived["next_cursor"] is None
            bad_cursor = await client.get("/toy/companion/private-world/life?history=1&before=bad", headers={"Origin": "https://client.example"})
            assert bad_cursor.status == 400
            preflight = await client.options("/toy/companion/private-world/life", headers={"Origin": "https://client.example"})
            assert preflight.status == 204
            assert "X-Olivia-Companion-Action" in preflight.headers["Access-Control-Allow-Headers"]
            refreshed = await client.post("/toy/companion/private-world/life", headers={"Origin": "https://client.example", "X-Olivia-Companion-Action": "confirmed"}, json={})
            assert refreshed.status == 200
            assert (await refreshed.json())["current"] == data["current"]
            forbidden = await client.post("/toy/companion/private-world/life", headers={"Origin": "https://evil.example"}, json={})
            assert forbidden.status == 403
    asyncio.run(run())


@pytest.mark.parametrize('emotion_state', ['missing', 'persisted', 'warm_missing_affect'])
def test_life_get_remains_readable_during_a_pending_write(tmp_path, emotion_state):
    """GET must not need the write lock, even for its first emotion projection."""
    async def run():
        from runtime.private_world.character_emotion import CharacterEmotionStore
        from runtime.private_world.current_affect import CurrentAffect
        store = DailyLifeStore(tmp_path / 'life.sqlite3')
        now = datetime.now(timezone.utc)
        store.publish_day('day:readable', {'location':'琴房', 'activity':'练琴',
                          'note':'今天想把这段弹稳。'}, [], occurred_at=now)
        def forbidden():
            pytest.fail('a world GET must not request a provider')
        life = DailyLifeRuntime(store, forbidden, lambda: '[]', relationship=PrivateWorldSnapshot)
        if emotion_state == 'persisted':
            CharacterEmotionStore(store.path)
            CurrentAffect(store)
            payload = dict(label='calm', as_of=now.isoformat(), reason='练习有了进展', basis={})
            with closing(sqlite3.connect(store.path)) as db, db:
                db.execute('INSERT INTO character_current_affect VALUES (1,?,?,?,?,?)',
                           (json.dumps(payload), 'fixture', now.timestamp(), 0, 0))
        elif emotion_state == 'warm_missing_affect':
            assert life.emotion is not None  # Its current-affect store is still absent.
        async def fallback(request):
            return web.Response(status=404)
        runtime = create_original_client_server_runtime(fallback, daily_life=life,
                              trusted_origins=('https://client.example',))
        async with TestClient(TestServer(runtime.app)) as client:
            with closing(sqlite3.connect(store.path)) as writer:
                writer.execute('BEGIN IMMEDIATE')
                before = writer.execute('SELECT name FROM sqlite_master ORDER BY name').fetchall()
                started = time.monotonic()
                response = await client.get('/toy/companion/private-world/life',
                                           headers={'Origin':'https://client.example'})
                elapsed = time.monotonic() - started
                value = await response.json()
                assert response.status == 200
                assert elapsed < 2, 'world read waited for the five-second write-lock budget'
                assert value['current']['source_id'] == 'day:readable'
                if emotion_state == 'persisted':
                    assert value['emotion']['current_affect']['label'] == 'calm'
                assert writer.execute('SELECT name FROM sqlite_master ORDER BY name').fetchall() == before
    asyncio.run(run())


def test_background_refresh_adapts_routine_without_a_world_page_read(tmp_path, monkeypatch):
    from runtime.reply import companion_duties, jev_questions
    monkeypatch.setattr(companion_duties, 'configured_duties', lambda: None)
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: None)
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    now = datetime(2026, 10, 7, 3, 30, tzinfo=timezone.utc)
    store.publish_day('day:fresh', {'location':'琴房', 'activity':'练琴',
                      'note':'今天想把这段弹稳。'}, [], occurred_at=now)
    life = DailyLifeRuntime(store, lambda: None, lambda: '[]', relationship=PrivateWorldSnapshot)
    with store._db() as db:
        assert db.execute('SELECT COUNT(*) FROM life_routine_days').fetchone()[0] == 0
    asyncio.run(life.refresh(now))
    with store._db() as db:
        assert db.execute('SELECT COUNT(*) FROM life_routine_days').fetchone()[0] == 1
