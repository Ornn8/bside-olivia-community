"""Invitations remain expressive; facts and executable permissions stay grounded."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from persona_assembly import assemble_persona
from persona_loader import load_persona
from runtime.model_policy import PREFIX, encode_system, encode_text
from runtime.persona.persona_assembly import _FORBIDDEN_RULES
from runtime.personal_chat.decision import INSTRUCTION, decode
from runtime.reply.jev_quality import _CODE_RULES
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime


def test_release_invitation_keeps_facts_and_permissions_separate():
    root = Path(__file__).resolve().parents[2]
    snapshot = load_persona(root / 'linli_character/persona_release_v2.json').snapshot
    context = ReplyContext.create(ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 10, 16, tzinfo=timezone.utc)))
    system = assemble_persona(snapshot, context, user_input='周末约会好吗？', max_units=30000).system_content
    assert '可以约、期待和商量安排' in system
    assert '不定具体时间地点' not in system
    assert '未成年人或有未成年迹象时不暧昧、不亲密' in system
    assert '不写露骨性内容' in system
    assert '未成功交付前不说已发出' in system
    assert '不补用户没说过的细节' in system
    private = json.loads(re.search(r'<private_behavior>\s*(.*?)\s*</private_behavior>', system, re.S)[1])
    assert private['action_permissions']['physical_contact'] == {'ceiling': 'none', 'granted': 'none'}
    assert private['action_permissions']['current_home_access'] == {'allowed': False}
    assert PREFIX in encode_system(system)


def test_new_wording_keeps_server_policy_references():
    for text in (_FORBIDDEN_RULES[2], _FORBIDDEN_RULES[7], _CODE_RULES['BOUNDARY_BREACH'], _CODE_RULES['STAGE_DRIFT']):
        assert encode_text(text).startswith(PREFIX)
    encoded = encode_text(INSTRUCTION)
    # New local silence controls must not be replaced by an older server envelope.
    assert '只有当前用户明确要求先等其后续消息或无需回答时' in encoded
    assert 'daily_video_candidates' in encoded


def test_warm_future_invitation_does_not_create_a_scheduled_task():
    raw = json.dumps({'text': '好呀，想到一起看日落就开心。'})
    result = decode(raw, user='想和你看日落', now=1792108800)
    assert result is not None
    assert result['followup_at'] is None
    assert result['initiative'] == 'keep'
