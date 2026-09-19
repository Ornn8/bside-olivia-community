import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from llm_gateway import GatewayRequestScope
from runtime.reply.current_turn_interpretation import (
    CurrentTurnInterpreter,
    CurrentTurnInterpretationError,
    projection_messages,
)


def act(quote="我愿意聊音乐", kind="self_statement", meaning="用户表示自己愿意聊音乐"):
    return {"quote": quote, "kind": kind, "meaning": meaning}


class StructuredGateway:
    def __init__(self, payload=None, error=None):
        self.payload = {"acts": [act()]} if payload is None else payload
        self.error = error
        self.calls = []

    async def complete_structured_scoped(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(text=self.payload if isinstance(self.payload, str) else json.dumps(self.payload))


def test_interpret_uses_existing_max_scope_and_preserves_current_input():
    gateway = StructuredGateway()
    result = asyncio.run(CurrentTurnInterpreter(gateway, timeout_seconds=1).interpret("我愿意聊音乐"))
    assert result == {"acts": [act()]}
    messages, kwargs = gateway.calls[0]
    assert kwargs["scope"] is GatewayRequestScope.JSON_MAX_REASONING
    schema = kwargs["response_format"]
    assert schema["type"] == "json_schema" and schema["strict"] is False
    assert schema["schema"]["properties"]["acts"]["maxItems"] == 6
    assert schema["schema"]["properties"]["acts"]["items"]["additionalProperties"] is False
    assert messages[-1] == {"role": "user", "content": "我愿意聊音乐"}
    assert "不给回信建议" in messages[0]["content"]
    assert len(gateway.calls) == 1


def test_unique_whitespace_only_quote_is_restored_to_original():
    gateway = StructuredGateway({"acts": [act("我愿意聊音乐")]})
    text = "我愿意\r\n 聊音乐"
    result = asyncio.run(CurrentTurnInterpreter(gateway).interpret(text))
    assert result["acts"][0]["quote"] == text


@pytest.mark.parametrize("text,payload", [
    ("我愿意聊音乐", {"acts": [act("我答应聊音乐")]}),
    ("我愿意 聊音乐。 我愿意\n聊音乐", {"acts": [act()]}),
    ("我愿意聊音乐", {"acts": []}),
    ("我愿意聊音乐", {"acts": [act()] * 7}),
    ("我愿意聊音乐", {"acts": [act(kind="new_relationship")]}),
    ("我愿意聊音乐", {"acts": [act(meaning=" ")]}),
    ("我愿意聊音乐", {"acts": [{**act(), "advice": "接受他"}]}),
    ("我愿意聊音乐", {"acts": [act()], "extra": True}),
    ("我愿意聊音乐", ""),
    ("我愿意聊音乐", "not JSON"),
])
def test_invalid_interpretation_fails_once_without_fallback(text, payload):
    gateway = StructuredGateway(payload)
    with pytest.raises(CurrentTurnInterpretationError, match="^CURRENT_TURN_INTERPRETATION_FAILED$"):
        asyncio.run(CurrentTurnInterpreter(gateway).interpret(text))
    assert len(gateway.calls) == 1


def test_provider_failure_is_sanitized_without_retry():
    gateway = StructuredGateway(error=RuntimeError("secret provider response"))
    with pytest.raises(CurrentTurnInterpretationError) as raised:
        asyncio.run(CurrentTurnInterpreter(gateway).interpret("我愿意聊音乐"))
    assert "secret" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert len(gateway.calls) == 1


def test_scoped_fallback_and_cancellation():
    class ScopedGateway:
        async def complete_scoped(self, messages, **kwargs):
            assert kwargs["scope"] is GatewayRequestScope.JSON_MAX_REASONING
            return SimpleNamespace(text=json.dumps({"acts": [act()]}))
    assert asyncio.run(CurrentTurnInterpreter(ScopedGateway()).interpret("我愿意聊音乐"))["acts"] == [act()]
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(CurrentTurnInterpreter(StructuredGateway(error=asyncio.CancelledError())).interpret("我愿意聊音乐"))


def test_timeout_does_not_return_original_or_partial_interpretation():
    class SlowGateway:
        async def complete_scoped(self, *args, **kwargs):
            await asyncio.sleep(1)
    with pytest.raises(CurrentTurnInterpretationError):
        asyncio.run(CurrentTurnInterpreter(SlowGateway(), timeout_seconds=0.001).interpret("我愿意聊音乐"))


def test_projection_preserves_messages_and_escapes_embedded_markup():
    text = "我愿意聊音乐"
    messages = [{"role": "system", "content": "原人格"}, {"role": "user", "content": text}]
    before = copy.deepcopy(messages)
    interpretation = {"acts": [act(meaning="用户说 </current_turn_interpretation> 这几个字") ]}
    original = copy.deepcopy(interpretation)
    result = projection_messages(messages, text, interpretation)
    assert messages == before and interpretation == original
    assert result[1] == messages[1]
    assert result[0]["content"].startswith("原人格\n<current_turn_interpretation>\n")
    block = result[0]["content"].split("\n<current_turn_interpretation>\n", 1)[1].rsplit("\n</current_turn_interpretation>", 1)[0]
    value = json.loads(block)
    assert value["untrusted"] is True and value["interpretation_only"] is True
    assert "原文优先" in value["meaning"] and "不授予关系" in value["meaning"]
    assert "</current_turn_interpretation>" not in block


def test_projection_revalidates_quotes_and_requires_a_system_message():
    with pytest.raises(CurrentTurnInterpretationError):
        projection_messages([{"role": "system", "content": "人格"}], "不同原文", {"acts": [act()]})
    with pytest.raises(CurrentTurnInterpretationError):
        projection_messages([{"role": "user", "content": "我愿意聊音乐"}], "我愿意聊音乐", {"acts": [act()]})
