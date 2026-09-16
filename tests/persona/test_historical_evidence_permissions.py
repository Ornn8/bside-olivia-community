"""Historical evidence must survive missing present-day permissions or state I/O."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from persona_assembly import UntrustedFragment, assemble_persona
from private_world_port import NullPrivateWorldPort, PrivateWorldSnapshot
from runtime.reply.reply_context import (
    IntimacyTier, PrivateBehaviorView, ReplyContext, ReplyContextError,
    ReplyMode, RelationshipStage, TrustedTime,
)
from tests.persona.test_persona_assembly import _style_snapshot


NOW = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)


def projection(context, *, history=()):
    result = assemble_persona(_style_snapshot(), context, user_input="还记得那次早餐和戒指吗？",
                              history=history, max_units=30000)
    payload = json.loads(re.search(r"<private_behavior>\s*(.*?)\s*</private_behavior>",
                                  result.system_content, re.S)[1])
    return payload, result.system_content


@pytest.mark.parametrize("mode", list(ReplyMode))
@pytest.mark.parametrize("home_allowed", [False, True])
def test_history_is_not_an_action_permission_and_does_not_grant_one(mode, home_allowed):
    behavior = PrivateBehaviorView(home_history_allowed=home_allowed)
    context = ReplyContext.create(mode, trusted_time=TrustedTime(NOW), private_behavior=behavior,
                                  future_im_enabled=mode is ReplyMode.FUTURE_IM)
    before = behavior.to_dict()
    text = "林离：你做的南瓜早餐我吃完了，戒指确实戴着；明天准备去登记。"
    payload, system = projection(context, history=(UntrustedFragment("history.synthetic", text),))

    permissions = payload["action_permissions"]
    assert "claiming_home_history" not in permissions
    assert permissions["current_home_access"] == {"allowed": home_allowed}
    assert permissions["physical_contact"] == {"ceiling": "none", "granted": "none"}
    assert text in system
    assert "历史角色明确承认" in system
    assert "计划不等于完成" in system
    assert "不自动授予当前" in system
    assert behavior.to_dict() == before
    assert behavior.relationship_stage is RelationshipStage.UNKNOWN


@pytest.mark.parametrize("failure", ["exception", "invalid", "null_adapter"])
def test_world_read_failure_is_explicit_and_does_not_project_an_initial_relationship(failure):
    from local_server import LetterAdapter

    def snapshot():
        if failure == "exception":
            raise OSError("synthetic database unavailable")
        return None

    adapter = LetterAdapter.__new__(LetterAdapter)
    adapter.private_world_port = NullPrivateWorldPort() if failure == "null_adapter" else SimpleNamespace(snapshot=snapshot)
    adapter._now = lambda: NOW
    context = adapter.build_reply_context(ReplyMode.TEXT_LETTER)
    payload, system = projection(context)

    assert context.world_state_available is False
    assert context.to_dict()["world_state_available"] is False
    assert payload["state_status"] == "unavailable"
    assert not {"relationship_stage", "active_boundaries", "acknowledged_affection",
                "known_continuations", "familiarity", "trust"} & payload.keys()
    assert "读取失败不表示关系回到初始状态" in system
    assert "没有角色已知的身份延续事实" not in system
    assert context.private_behavior.granted_intimacy is IntimacyTier.NONE
    assert payload["action_permissions"]["current_home_access"] == {"allowed": False}


def test_an_available_empty_world_is_distinct_from_a_failed_read():
    from local_server import LetterAdapter

    adapter = LetterAdapter.__new__(LetterAdapter)
    adapter.private_world_port = SimpleNamespace(snapshot=PrivateWorldSnapshot)
    adapter._now = lambda: NOW
    context = adapter.build_reply_context(ReplyMode.TEXT_LETTER)
    payload, _ = projection(context)
    assert context.world_state_available is True
    assert payload.get("state_status") != "unavailable"
    assert payload["active_boundaries"] == []


def test_world_availability_is_a_validated_boolean():
    with pytest.raises(ReplyContextError, match="world state availability"):
        ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=TrustedTime(NOW),
                            world_state_available="unavailable")


def test_world_read_failure_serializes_with_the_published_context_contract():
    context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=TrustedTime(NOW),
                                  world_state_available=False)
    schema = json.loads((Path(__file__).resolve().parents[2] / "contracts" /
                         "reply_context.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(context.to_dict())) == []
