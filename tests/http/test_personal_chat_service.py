import asyncio
import json

import pytest

from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService


def test_two_channels_share_serialized_generation_and_commit(tmp_path):
    async def scenario():
        rows, seen, order = [], [], []
        def persist():
            (tmp_path / "state.json").write_text(json.dumps({"letters": [], "personal_chats": rows}), encoding="utf-8")
        async def generate(event, row):
            order.append("generate:" + event.channel)
            assert row["delivery_status"] == "GENERATING"
            assert len(seen) == (1 if event.channel == "qq" else 0)
            await asyncio.sleep(0)
            return "答复 " + event.text
        async def commit(row):
            persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
            assert persisted["letters"] == []
            assert persisted["personal_chats"][-1]["delivery_status"] == "DELIVERED"
            seen.append(row["letter_id"])
            order.append("commit:" + row["channel"])
        async def send(text):
            assert rows[-1]["delivery_status"] == "SENDING"
        service = PersonalChatService(rows, persist, generate, commit, {"wechat": ("a", "owner"), "qq": ("b", "owner")})
        await asyncio.gather(service.handle(PersonalMessage("wechat", "a", "owner", "1", "晚饭"), send),
                             service.handle(PersonalMessage("qq", "b", "owner", "1", "散步"), send))
        assert order == ["generate:wechat", "commit:wechat", "generate:qq", "commit:qq"]
        assert len(set(seen)) == 2
    asyncio.run(scenario())


def test_unknown_send_never_retried_or_committed_after_restart(tmp_path):
    async def scenario():
        rows, calls = [], []
        async def generate(event, row):
            calls.append("generate")
            return "答复"
        async def send(text):
            calls.append("send")
            raise TimeoutError()
        async def commit(row):
            calls.append("commit")
        event = PersonalMessage("qq", "b", "owner", "1", "你好")
        service = PersonalChatService(rows, lambda: None, generate, commit, {"qq": ("b", "owner")})
        with pytest.raises(TimeoutError):
            await service.handle(event, send)
        restarted = PersonalChatService(json.loads(json.dumps(rows)), lambda: None, generate, commit, service.bindings)
        await restarted.recover()
        with pytest.raises(RuntimeError, match="REQUIRES_ATTENTION"):
            await restarted.handle(event, send)
        assert calls == ["generate", "send"]
        assert rows[0]["letter_status"] != "COMPLETED"
    asyncio.run(scenario())


def test_delivered_replay_retries_consumer_without_resend_and_rejects_foreign_owner():
    async def scenario():
        rows, calls = [], []
        async def generate(event, row):
            calls.append("generate")
            return "答复"
        async def send(text):
            calls.append("send")
        async def commit(row):
            calls.append("commit")
            if calls.count("commit") == 1:
                raise RuntimeError("WORLD_UNAVAILABLE")
        service = PersonalChatService(rows, lambda: None, generate, commit, {"qq": ("b", "owner")})
        with pytest.raises(ValueError):
            await service.handle(PersonalMessage("qq", "b", "foreign", "1", "你好"), send)
        assert calls == [] and rows == []
        event = PersonalMessage("qq", "b", "owner", "1", "你好")
        with pytest.raises(RuntimeError):
            await service.handle(event, send)
        await service.handle(event, send)
        assert calls == ["generate", "send", "commit", "commit"]
    asyncio.run(scenario())


def test_generation_failure_is_retryable_but_bounded_and_never_silently_consumed():
    async def scenario():
        rows, calls = [], []
        async def generate(event, row):
            calls.append("generate")
            raise RuntimeError("SYNTHETIC_PROVIDER_FAILURE")
        async def unexpected(*args):
            raise AssertionError("no delivery allowed")
        service = PersonalChatService(rows, lambda: None, generate, unexpected, {"qq": ("b", "owner")})
        event = PersonalMessage("qq", "b", "owner", "1", "你好")
        for _ in range(2):
            with pytest.raises(RuntimeError, match="PROVIDER_FAILURE"):
                await service.handle(event, unexpected)
        with pytest.raises(RuntimeError, match="RETRY_EXHAUSTED"):
            await service.handle(event, unexpected)
        assert calls == ["generate", "generate"]
    asyncio.run(scenario())


def test_oversized_reply_fails_before_any_send_reservation():
    async def scenario():
        rows, states = [], []
        async def generate(event, row):
            return "x" * 10001
        async def unexpected(*args):
            raise AssertionError("no send allowed")
        service = PersonalChatService(rows, lambda: states.append(rows[0]["delivery_status"]), generate, unexpected, {"qq": ("b", "owner")})
        with pytest.raises(ValueError, match="REPLY_INVALID"):
            await service.handle(PersonalMessage("qq", "b", "owner", "1", "hi"), unexpected)
        assert "SENDING" not in states
    asyncio.run(scenario())
