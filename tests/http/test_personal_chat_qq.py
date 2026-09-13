import asyncio

from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from runtime.personal_chat.qq import run_qq as _run_qq
from functools import partial
run_qq = partial(_run_qq, merge_seconds=0)

TOKEN = "synthetic-onebot-token"


def event(identifier=1, **overrides):
    return {"post_type": "message", "message_type": "private", "self_id": 100,
        "user_id": 200, "message_id": identifier,
        "message": [{"type": "text", "data": {"text": "hello"}}], **overrides}


async def login(ws, account=100):
    request = await ws.receive_json()
    assert request["action"] == "get_login_info"
    await ws.send_json({"echo": request["echo"], "status": "ok", "retcode": 0,
        "data": {"user_id": account}})


def test_owner_filter_and_ack_reader_do_not_deadlock():
    async def scenario():
        stop = asyncio.Event()
        received, sent = [], []
        async def handler(message, send):
            received.append(message)
            assert await send("literal [CQ:at,qq=999] text") == "300"
            stop.set()
        async def socket(request):
            assert request.headers["Authorization"] == "Bearer " + TOKEN
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            for raw in [[], {"echo": []}, event(user_id=999), event(message_type="group"),
                        event(user_id=100), event(self_id=999), event()]:
                await ws.send_json(raw)
            reply = await ws.receive_json()
            sent.append(reply)
            await ws.send_json({"echo": "unrelated", "status": "ok", "retcode": 0, "data": {"message_id": 999}})
            await ws.send_json({"echo": reply["echo"], "status": "ok", "retcode": 0, "data": {"message_id": 300}})
            await stop.wait()
            await ws.close()
            return ws
        app = web.Application()
        app.router.add_get("/", socket)
        async with TestServer(app) as server:
            await asyncio.wait_for(run_qq(str(server.make_url("/")), TOKEN, "100", "200", handler, stop), 3)
        assert len(received) == len(sent) == 1
        assert sent[0]["params"] == {"user_id": 200, "message": [{"type": "text", "data": {"text": "literal [CQ:at,qq=999] text"}}]}
    asyncio.run(scenario())


@pytest.mark.parametrize("ack", [None, {"status": "failed", "retcode": 1, "data": {}},
    {"status": "ok", "retcode": 0, "data": {}},
    {"status": "ok", "retcode": 0, "data": {"message_id": True}}])
def test_uncertain_or_rejected_send_is_not_retried(ack):
    async def scenario():
        stop = asyncio.Event()
        sent = []
        async def handler(message, send):
            with pytest.raises((TimeoutError, RuntimeError)):
                await send("reply")
            stop.set()
        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            await ws.send_json(event())
            reply = await ws.receive_json()
            sent.append(reply)
            if ack is not None:
                await ws.send_json({**ack, "echo": reply["echo"]})
            await stop.wait()
            await ws.close()
            return ws
        app = web.Application()
        app.router.add_get("/", socket)
        async with TestServer(app) as server:
            await asyncio.wait_for(run_qq(str(server.make_url("/")), TOKEN, "100", "200", handler, stop, ack_timeout=.1), 3)
        assert len(sent) == 1
    asyncio.run(scenario())


def test_wrong_login_fails_closed_without_handler_or_reconnect():
    async def scenario():
        calls = []
        async def handler(*args):
            calls.append("handler")
        async def socket(request):
            calls.append("connect")
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws, 999)
            await ws.send_json(event())
            await ws.close()
            return ws
        app = web.Application()
        app.router.add_get("/", socket)
        async with TestServer(app) as server:
            with pytest.raises(ValueError, match="QQ_ACCOUNT_MISMATCH"):
                await run_qq(str(server.make_url("/")), TOKEN, "100", "200", handler, asyncio.Event())
        assert calls == ["connect"]
    asyncio.run(scenario())


def test_read_disconnect_reconnects_and_stops_cleanly():
    async def scenario():
        stop = asyncio.Event()
        connections = []
        async def handler(message, send):
            assert message.message_id == "2"
            stop.set()
        async def socket(request):
            connections.append(1)
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            if len(connections) == 2:
                await ws.send_json(event(2))
                await stop.wait()
            await ws.close()
            return ws
        app = web.Application()
        app.router.add_get("/", socket)
        async with TestServer(app) as server:
            await asyncio.wait_for(run_qq(str(server.make_url("/")), TOKEN, "100", "200", handler, stop, reconnect_delay=.01), 3)
        assert len(connections) == 2
    asyncio.run(scenario())


def test_handler_failure_is_visible_and_sanitized():
    async def scenario():
        async def handler(message, send):
            raise RuntimeError("private-message-or-key-must-not-escape")
        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            await ws.send_json(event())
            async for _ in ws:
                pass
            return ws
        app = web.Application()
        app.router.add_get("/", socket)
        async with TestServer(app) as server:
            with pytest.raises(RuntimeError, match="^QQ_MESSAGE_HANDLER_FAILED$"):
                await asyncio.wait_for(run_qq(str(server.make_url("/")), TOKEN, "100", "200", handler, asyncio.Event()), 3)
    asyncio.run(scenario())


def test_handlers_serialize_while_reader_receives_more_messages():
    async def scenario():
        stop = asyncio.Event()
        order = []
        async def handler(message, send):
            order.append("start-" + message.message_id)
            await send("reply-" + message.message_id)
            order.append("end-" + message.message_id)
            if message.message_id == "2":
                stop.set()
        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            await ws.send_json(event(1))
            reply = await ws.receive_json()
            await ws.send_json(event(2))
            await ws.send_json({"echo": reply["echo"], "status": "ok", "retcode": 0, "data": {"message_id": 3}})
            reply = await ws.receive_json()
            await ws.send_json({"echo": reply["echo"], "status": "ok", "retcode": 0, "data": {"message_id": 4}})
            await stop.wait()
            await ws.close()
            return ws
        app = web.Application()
        app.router.add_get("/", socket)
        async with TestServer(app) as server:
            await asyncio.wait_for(run_qq(str(server.make_url("/")), TOKEN, "100", "200", handler, stop), 3)
        assert order == ["start-1", "end-1", "start-2", "end-2"]
    asyncio.run(scenario())


def test_disconnect_during_send_is_visible_and_never_resends():
    async def scenario():
        sends = []
        async def handler(message, send):
            await send("uncertain")
        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            await ws.send_json(event())
            sends.append(await ws.receive_json())
            await ws.close()
            return ws
        app = web.Application()
        app.router.add_get("/", socket)
        async with TestServer(app) as server:
            with pytest.raises(RuntimeError, match="^QQ_CONNECTION_LOST_DURING_EXCHANGE$"):
                await asyncio.wait_for(run_qq(str(server.make_url("/")), TOKEN, "100", "200", handler, asyncio.Event(), reconnect_delay=.01), 3)
        assert len(sends) == 1
    asyncio.run(scenario())
