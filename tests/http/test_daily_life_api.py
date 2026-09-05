import asyncio
from datetime import datetime, timezone

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from original_client_server import create_original_client_server_runtime
from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime


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
