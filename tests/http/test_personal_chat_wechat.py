import asyncio

import aiohttp
import pytest

from runtime.personal_chat import wechat


CREDENTIALS = {"base": "https://ilinkai.weixin.qq.com", "token": "synthetic",
               "account": "bot", "owner": "owner"}


def message(identifier, owner="owner"):
    return {"message_id": identifier, "from_user_id": owner, "to_user_id": "bot",
            "message_type": 1, "message_state": 2, "context_token": "private-context",
            "item_list": [{"type": 1, "text_item": {"text": "hello"}}]}


class Cursor:
    def __init__(self):
        self.value = "old"

    def load(self):
        return self.value

    def save(self, value):
        self.value = value


def test_owner_only_send_and_cursor_saved_after_checked_ack(monkeypatch):
    async def exercise():
        stop, cursor, sends = asyncio.Event(), Cursor(), []

        async def request(session, base, path, **kwargs):
            if path.endswith("getupdates"):
                assert kwargs["body"] == {"get_updates_buf": "old"}
                return {"msgs": [message(1, "stranger"), message(2)], "get_updates_buf": "new"}
            assert cursor.value == "old"
            sends.append(kwargs["body"]["msg"])
            return {}  # Official endpoint permits absent ret on success.

        async def handle(event, send):
            assert event.owner_id == "owner"
            assert await send("  reply\ntext  ") == {}
            with pytest.raises(RuntimeError, match="ALREADY_ATTEMPTED"):
                await send("again")
            stop.set()

        monkeypatch.setattr(wechat, "wechat_request", request)
        await wechat.run_wechat(CREDENTIALS, handle, stop, cursor_store=cursor)
        assert cursor.value == "new"
        assert len(sends) == 1
        assert sends[0]["to_user_id"] == "owner"
        assert sends[0]["context_token"] == "private-context"
        assert sends[0]["item_list"][0]["text_item"]["text"] == "  reply\ntext  "

    asyncio.run(exercise())


def test_failed_handler_leaves_batch_replayable(monkeypatch):
    async def exercise():
        cursor, handled = Cursor(), []

        async def request(*args, **kwargs):
            return {"msgs": [message(1), message(2), message(3)], "get_updates_buf": "new"}

        async def handle(event, send):
            handled.append(event.message_id)
            if event.message_id == "2":
                raise RuntimeError("service unavailable")

        monkeypatch.setattr(wechat, "wechat_request", request)
        with pytest.raises(RuntimeError, match="service unavailable"):
            await wechat.run_wechat(CREDENTIALS, handle, asyncio.Event(), cursor_store=cursor)
        assert cursor.value == "old"
        assert handled == ["1", "2"]

    asyncio.run(exercise())


def test_unknown_send_is_not_retried(monkeypatch):
    async def exercise():
        stop, attempts = asyncio.Event(), []

        async def request(session, base, path, **kwargs):
            if path.endswith("getupdates"):
                return {"msgs": [message(1)]}
            attempts.append(path)
            raise asyncio.TimeoutError

        async def handle(event, send):
            with pytest.raises(asyncio.TimeoutError):
                await send("reply")
            with pytest.raises(RuntimeError, match="ALREADY_ATTEMPTED"):
                await send("retry")
            stop.set()

        monkeypatch.setattr(wechat, "wechat_request", request)
        await wechat.run_wechat(CREDENTIALS, handle, stop)
        assert len(attempts) == 1

    asyncio.run(exercise())


def test_stop_cancels_long_poll(monkeypatch):
    async def exercise():
        stop, polling, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def request(*args, **kwargs):
            polling.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(wechat, "wechat_request", request)
        task = asyncio.create_task(wechat.run_wechat(CREDENTIALS, None, stop))
        await polling.wait()
        stop.set()
        await asyncio.wait_for(task, 1)
        assert cancelled.is_set()

    asyncio.run(exercise())


def test_read_disconnect_retries_same_cursor(monkeypatch):
    async def exercise():
        stop, reads = asyncio.Event(), []

        async def request(session, base, path, **kwargs):
            reads.append(kwargs["body"]["get_updates_buf"])
            if len(reads) == 1:
                raise aiohttp.ClientConnectionError("synthetic")
            return {"msgs": [message(1)], "get_updates_buf": "new"}

        async def handle(event, send):
            stop.set()

        monkeypatch.setattr(wechat, "wechat_request", request)
        await wechat.run_wechat(CREDENTIALS, handle, stop, cursor_store=Cursor())
        assert reads == ["old", "old"]

    asyncio.run(exercise())


def test_missing_context_does_not_advance_cursor(monkeypatch):
    async def exercise():
        cursor = Cursor()
        raw = message(1)
        del raw["context_token"]

        async def request(*args, **kwargs):
            return {"msgs": [raw], "get_updates_buf": "new"}

        monkeypatch.setattr(wechat, "wechat_request", request)
        with pytest.raises(RuntimeError, match="CONTEXT_MISSING"):
            await wechat.run_wechat(CREDENTIALS, None, asyncio.Event(), cursor_store=cursor)
        assert cursor.value == "old"

    asyncio.run(exercise())


def test_authorization_rejection_stops_without_retry_or_secret_error(monkeypatch):
    async def exercise():
        calls = []

        async def request(*args, **kwargs):
            calls.append(1)
            raise aiohttp.ClientResponseError(None, (), status=401, message="private-token")

        monkeypatch.setattr(wechat, "wechat_request", request)
        with pytest.raises(RuntimeError, match="WECHAT_POLL_REJECTED") as caught:
            await wechat.run_wechat(CREDENTIALS, None, asyncio.Event())
        assert "private-token" not in str(caught.value)
        assert len(calls) == 1

    asyncio.run(exercise())
