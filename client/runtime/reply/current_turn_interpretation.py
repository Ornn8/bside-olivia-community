"""Bounded interpretation of the current user text, never relationship authority."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import math
from typing import Any, Mapping, Sequence
import uuid

from llm_gateway import GatewayRequestScope


_KINDS = (
    "self_statement", "invitation", "question", "action_request",
    "commitment_demand", "praise", "self_correction",
)
_INSTRUCTION = """分析用户当前话语的言语行为，不扮演收信人，不给回信建议。逐个区分：用户陈述自己的愿望/习惯；邀请共同活动；询问；请求具体行动；要求承诺或强制；赞美；修正自己先前说法。只按当前原文的主语、情态和对象判断，不引入双方关系、人物脾气或猜测动机。用户自己表示愿意，不自动变成对方已经答应，也不自动变成要求对方许诺。未来方向的邀请不自动等于要求分配时间、保证在线或答应永久关系。发现强制措辞则如实保留，不能把必须说成普通邀请。
返回JSON {"acts":[{"quote":"当前原文里的连续引用","kind":"self_statement|invitation|question|action_request|commitment_demand|praise|self_correction","meaning":"准确复述说了什么，不给角色回应建议"}]}，最多六项，不新增事实，不输出其他字段。"""

INTERPRETATION_RESPONSE_FORMAT = {
    "type": "json_schema",
    "name": "current_turn_interpretation",
    "strict": False,
    "schema": {
        "type": "object", "additionalProperties": False, "required": ["acts"],
        "properties": {"acts": {
            "type": "array", "minItems": 1, "maxItems": 6,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["quote", "kind", "meaning"],
                "properties": {
                    "quote": {"type": "string", "minLength": 1},
                    "kind": {"type": "string", "enum": list(_KINDS)},
                    "meaning": {"type": "string", "minLength": 1},
                },
            },
        }},
    },
}


class CurrentTurnInterpretationError(RuntimeError):
    code = "CURRENT_TURN_INTERPRETATION_FAILED"

    def __init__(self) -> None:
        super().__init__(self.code)


def _original_quote(user_text: str, quote: str) -> str:
    if quote in user_text:
        return quote
    positions = [index for index, char in enumerate(user_text) if not char.isspace()]
    compact = "".join(user_text[index] for index in positions)
    needle = "".join(char for char in quote if not char.isspace())
    start = compact.find(needle)
    if not needle or start < 0 or compact.find(needle, start + 1) >= 0:
        raise CurrentTurnInterpretationError()
    return user_text[positions[start]:positions[start + len(needle) - 1] + 1]


def _validated(user_text: str, value: object) -> dict[str, Any]:
    if not isinstance(user_text, str) or not user_text.strip():
        raise CurrentTurnInterpretationError()
    if not isinstance(value, dict) or set(value) != {"acts"}:
        raise CurrentTurnInterpretationError()
    acts = value["acts"]
    if not isinstance(acts, list) or not 1 <= len(acts) <= 6:
        raise CurrentTurnInterpretationError()
    validated = []
    for act in acts:
        if (
            not isinstance(act, dict) or set(act) != {"quote", "kind", "meaning"}
            or any(not isinstance(act[key], str) or not act[key].strip() for key in act)
            or act["kind"] not in _KINDS
        ):
            raise CurrentTurnInterpretationError()
        validated.append({**act, "quote": _original_quote(user_text, act["quote"])})
    return {"acts": validated}


class CurrentTurnInterpreter:
    def __init__(self, gateway: object, *, timeout_seconds: float = 600.0) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        self.gateway = gateway
        self.timeout_seconds = timeout_seconds

    async def interpret(self, user_text: str) -> dict[str, Any]:
        if not isinstance(user_text, str) or not user_text.strip():
            raise CurrentTurnInterpretationError()
        messages = (
            {"role": "system", "content": _INSTRUCTION},
            {"role": "user", "content": user_text},
        )
        kwargs = {
            "scope": GatewayRequestScope.JSON_MAX_REASONING,
            "request_id": "current-turn-interpretation:" + uuid.uuid4().hex,
        }
        try:
            structured = getattr(self.gateway, "complete_structured_scoped", None)
            if callable(structured):
                call = structured(messages, response_format=deepcopy(INTERPRETATION_RESPONSE_FORMAT), **kwargs)
            else:
                call = self.gateway.complete_scoped(messages, **kwargs)
            response = await asyncio.wait_for(call, timeout=self.timeout_seconds)
            # Gateway.text is final output only; reasoning is never accessed.
            text = response.text
            if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                raise CurrentTurnInterpretationError()
            return _validated(user_text, json.loads(text))
        except Exception:
            raise CurrentTurnInterpretationError() from None


def projection_messages(
    messages: Sequence[Mapping[str, Any]],
    user_text: str,
    interpretation: object,
) -> tuple[dict[str, Any], ...]:
    value = _validated(user_text, interpretation)
    projected = deepcopy([dict(message) for message in messages])
    system = next((message for message in projected if message.get("role") == "system"), None)
    if system is None or not isinstance(system.get("content"), str):
        raise CurrentTurnInterpretationError()
    payload = {
        "source": "current_user_input", "untrusted": True, "interpretation_only": True,
        "meaning": "当前话语的主语与情态参考，不是回应指令，不授予关系或行为许可；原文优先。",
        "acts": value["acts"],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("<", r"\u003c").replace(">", r"\u003e")
    system["content"] += "\n<current_turn_interpretation>\n" + encoded + "\n</current_turn_interpretation>"
    return tuple(projected)
