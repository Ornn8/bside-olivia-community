import asyncio
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer
import pytest

from runtime.personal_chat.probe import ProbeJournal, checked_url, qq


@pytest.mark.parametrize("ack,passes", [({}, True), ({"ret": 0}, True),
    ({"ret": -1}, False), ({"errcode": -14}, False)])
def test_wechat_send_ack_matches_official_optional_ret(tmp_path, monkeypatch, ack, passes):
    from runtime.personal_chat import probe
    import original_client_setup_api as setup
    import json
    monkeypatch.setattr(setup, "_dpapi_unprotect", lambda value: value)
    (tmp_path / "wechat.dpapi").write_text(json.dumps({"token": "synthetic", "base":
        "https://ilinkai.weixin.qq.com", "owner": "owner", "account": "bot"}), encoding="utf-8")
    calls = []
    async def request(session, base, path, **kwargs):
        calls.append(path)
        if path.endswith("getupdates"):
            return {"msgs": [{"from_user_id": "owner", "to_user_id": "bot", "message_id": "one",
                "message_type": 1, "message_state": 2, "context_token": "synthetic-context",
                "item_list": [{"type": 1, "text_item": {"text": "/连接测试"}}]}]}
        # The shared HTTP wrapper rejects explicit business errors.
        if ack.get("ret", 0) != 0 or ack.get("errcode", 0) != 0:
            raise RuntimeError("WECHAT_API_REJECTED")
        return ack
    monkeypatch.setattr(probe, "wechat_request", request)
    journal = ProbeJournal(tmp_path)
    args = SimpleNamespace(data=tmp_path, login=False, polls=1)
    try:
        if passes:
            asyncio.run(probe.wechat(args, None, journal))
        else:
            with pytest.raises(RuntimeError):
                asyncio.run(probe.wechat(args, None, journal))
        assert journal.db.execute("SELECT state FROM sent").fetchall() == [("sent" if passes else "sending",)]
        assert calls.count("/ilink/bot/sendmessage") == 1
    finally:
        journal.db.close()


def test_wechat_accepts_live_binary_mime_json_and_checks_business_result(monkeypatch):
    from runtime.personal_chat import probe
    monkeypatch.setattr(probe, "checked_url", lambda value: value)
    async def scenario():
        async def endpoint(request):
            assert request.headers["Authorization"] == "Bearer synthetic-test-token"
            assert (await request.json())["base_info"]["bot_agent"] == "OliviaTransportProbe/0.1"
            return web.Response(body=b'{"ret":0,"msgs":[]}', content_type="application/octet-stream")
        app = web.Application()
        app.router.add_post("/ilink/bot/getupdates", endpoint)
        async with TestServer(app) as server, ClientSession() as client:
            result = await probe.wechat_request(client, str(server.make_url("/")).rstrip("/"),
                "/ilink/bot/getupdates", token="synthetic-test-token", body={"get_updates_buf": ""})
            assert result == {"ret": 0, "msgs": []}
    asyncio.run(scenario())


@pytest.mark.parametrize("url", ["https://evil.example", "http://ilinkai.weixin.qq.com",
    "https://weixin.qq.com.evil.example", "https://a.weixin.qq.com:444", "https://u:p@a.weixin.qq.com"])
def test_rejects_untrusted_wechat_endpoints(url):
    with pytest.raises(ValueError):
        checked_url(url)


def test_journal_does_not_resend_unknown_delivery_after_restart(tmp_path):
    first = ProbeJournal(tmp_path)
    assert first.reserve("one")
    first.db.close()
    second = ProbeJournal(tmp_path)
    assert not second.reserve("one")
    second.delivered("one")
    assert not second.reserve("one")
    second.db.close()


def test_real_websocket_owner_filter_and_platform_ack(tmp_path, monkeypatch):
    fixture_token = "synthetic-token-123456789"
    monkeypatch.setenv("OLIVIA_ONEBOT_TOKEN", fixture_token)
    async def scenario():
        sent = []
        async def socket(request):
            assert request.headers["Authorization"] == f"Bearer {fixture_token}"
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            assert (await ws.receive_json())["action"] == "get_login_info"
            await ws.send_json({"echo": "login", "status": "ok", "retcode": 0, "data": {"user_id": 100}})
            base = {"post_type": "message", "message_type": "private", "self_id": 100,
                    "message": [{"type": "text", "data": {"text": "/连接测试"}}]}
            await ws.send_json({**base, "user_id": 999, "message_id": 1})
            await ws.send_json({**base, "user_id": 200, "message_type": "group", "message_id": 2})
            await ws.send_json({**base, "user_id": 200, "message_id": 3})
            reply = await ws.receive_json()
            sent.append(reply)
            assert reply["params"]["user_id"] == 200
            await ws.send_json({"echo": reply["echo"], "status": "ok", "retcode": 0, "data": {"message_id": 4}})
            await ws.close()
            return ws
        app = web.Application()
        app.router.add_get("/", socket)
        async with TestServer(app) as server, ClientSession() as client:
            journal = ProbeJournal(tmp_path)
            try:
                args = SimpleNamespace(owner="200", account="100", onebot_url=str(server.make_url("/")).replace("http:", "ws:"))
                await asyncio.wait_for(qq(args, client, journal), 5)
                assert len(sent) == 1
                assert journal.db.execute("SELECT state FROM sent").fetchall() == [("sent",)]
            finally:
                journal.db.close()
    asyncio.run(scenario())
