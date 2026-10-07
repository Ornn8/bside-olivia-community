import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re

import pytest

from runtime.persona.persona_assembly import assemble_persona, UntrustedFragment
from runtime.persona.persona_loader import load_persona
from runtime.reply.prompt_budget import PromptBudgetExceeded
from runtime.reply.reply_context import BehaviorLevel, PrivateBehaviorView, ReplyContext, ReplyMode, TrustedTime


def test_changing_clock_relationship_and_life_preserves_fixed_persona_prefix():
    snapshot = load_persona(Path(__file__).resolve().parents[2] / "linli_character/persona_release_v2.json").snapshot
    results = []
    for minute, trust in ((1, BehaviorLevel.LOW), (2, BehaviorLevel.HIGH)):
        context = ReplyContext.create(
            ReplyMode.TEXT_LETTER,
            trusted_time=TrustedTime(datetime(2026, 9, 10, 8, minute, tzinfo=timezone.utc)),
            private_behavior=PrivateBehaviorView(trust=trust),
        )
        result = assemble_persona(
            snapshot, context, user_input="今天很平常。", max_units=30000,
            relationship_expression_enabled=True,
            evidence_summaries=(UntrustedFragment("linli.daily-life", f"合成近况 {minute}"),),
        )
        mode = json.loads(re.search(r"<mode_constraints>\s*(.*?)\s*</mode_constraints>", result.system_content, re.S)[1])
        assert "trusted_time" not in mode and "character_local_time" not in mode
        clock = json.loads(re.search(r"<runtime_time>\s*(.*?)\s*</runtime_time>", result.system_content, re.S)[1])
        assert clock["trusted_time"] == context.to_dict()["trusted_time"]
        assert clock["character_local_time"] == f"2026-09-10T16:0{minute}:00+08:00"
        assert f"合成近况 {minute}" in result.system_content
        assert "runtime_time" in result.budget_report.included_ids
        assert result.budget_report.used_units == len(result.system_content) + len(result.user_content)
        results.append(result)

    common = os.path.commonprefix([result.system_content for result in results])
    for declaration in snapshot.declarations:
        if declaration.inclusion == "phase":
            assert declaration.declaration_id not in common
        elif declaration.tier == "PUBLIC_CANON":
            assert declaration.declaration_id in common
    assert "<private_behavior>" not in common
    assert "合成近况" not in common
    assert results[0].system_content != results[1].system_content


def test_clock_remains_required_even_when_optional_context_is_dropped():
    snapshot = load_persona(Path(__file__).resolve().parents[2] / "linli_character/persona_release_v2.json").snapshot
    context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=TrustedTime(datetime(2026, 9, 10, tzinfo=timezone.utc)))
    with pytest.raises(PromptBudgetExceeded) as caught:
        assemble_persona(snapshot, context, user_input="你好", max_units=100)
    assert "runtime_time" in caught.value.report.included_ids


@pytest.mark.parametrize('channel', ['qq', 'wechat'])
def test_chat_delivery_changes_do_not_break_persona_cache_prefix(channel):
    from runtime.personal_chat.presentation import CURRENT

    snapshot = load_persona(Path(__file__).resolve().parents[2] / "linli_character/persona_release_v2.json").snapshot
    results = []
    for minute, voice in ((1, False), (2, True)):
        delivery = {'channel': channel, 'decision_now': f'2026-09-23T22:0{minute}:00',
                    'voice_available': voice, 'letter_invitation_allowed': voice}
        token = CURRENT.set(delivery)
        try:
            context = ReplyContext.create(ReplyMode.FUTURE_IM, future_im_enabled=True,
                trusted_time=TrustedTime(datetime(2026, 9, 23, 14, minute, tzinfo=timezone.utc)))
            result = assemble_persona(snapshot, context, user_input='今天怎么样？', max_units=30000)
            prefix, dynamic = result.system_content.split('<runtime_time>', 1)
            assert 'decision_now' not in prefix
            assert json.loads(re.search(r'<chat_delivery>\s*(.*?)\s*</chat_delivery>', dynamic, re.S)[1]) == delivery
            assert 'chat_delivery' in result.budget_report.included_ids
            assert result.budget_report.used_units == len(result.system_content) + len(result.user_content)
            results.append(prefix)
            with pytest.raises(PromptBudgetExceeded) as caught:
                assemble_persona(snapshot, context, user_input='你好', max_units=100)
            assert 'chat_delivery' in caught.value.report.included_ids
        finally:
            CURRENT.reset(token)
    assert results[0] == results[1]
    for declaration in snapshot.declarations:
        if declaration.inclusion == 'phase':
            assert declaration.declaration_id not in results[0]
        elif declaration.tier == 'PUBLIC_CANON':
            assert declaration.declaration_id in results[0]


def test_chat_rules_move_into_cached_prefix_with_reminder_by_input():
    from runtime.personal_chat.decision import INSTRUCTION
    from runtime.reply.fact_attribution import cache_output_rules, CACHED_RULES_REMINDER
    grounding = '<relationship_grounding>\n"stable rule"\n</relationship_grounding>\n'
    messages = ({'role': 'system', 'content': '<persona>\nP\n</persona>\n<runtime_time>\n{}\n</runtime_time>\n'
                                             + grounding + '<evidence_summary>\nE\n</evidence_summary>\n'},
                {'role': 'user', 'content': '[历史消息 {}]\n早'}, {'role': 'assistant', 'content': '[历史消息 {}]\n早呀'},
                {'role': 'system', 'content': INSTRUCTION}, {'role': 'user', 'content': '在吗'})
    result = cache_output_rules(messages, INSTRUCTION)
    prefix = result[0]['content']
    assert INSTRUCTION in prefix and grounding in prefix and '<runtime_time>' not in prefix
    # Dialogue first, then the per-turn state, then the reminder and the input.
    assert [m['content'][:6] for m in result[1:3]] == ['[历史消息 ', '[历史消息 ']
    assert result[3]['content'].startswith('<runtime_time>\n') and '<evidence_summary>' in result[3]['content']
    assert grounding not in result[3]['content']
    assert result[4] == {'role': 'system', 'content': CACHED_RULES_REMINDER}
    assert result[-1] == messages[-1] and len(result) == len(messages) + 1
    # Without the clock boundary nothing is moved.
    plain = ({'role': 'system', 'content': 'P'}, {'role': 'system', 'content': INSTRUCTION}, {'role': 'user', 'content': 'x'})
    assert cache_output_rules(plain, INSTRUCTION) == plain


def test_decision_in_single_item_array_is_accepted():
    from datetime import datetime, timezone
    from runtime.personal_chat.decision import decode
    body = {"text": "在呢", "delivery": "text", "listening": "keep", "initiative": "keep", "pause_until": None,
            "letter": "keep", "letter_until": None, "followup_at": None, "evidence": "", "sticker": None, "skip": False}
    now = datetime(2026, 9, 30, 13, tzinfo=timezone.utc)
    assert decode(json.dumps([body]), user='在吗', now=now)['text'] == '在呢'
    with pytest.raises(ValueError):
        decode(json.dumps([body, body]), user='在吗', now=now)


def test_chat_window_start_moves_in_steps_so_the_dialogue_prefix_repeats():
    from runtime.personal_chat.context import READ_WINDOW
    from runtime.reply.conversation_context import conversation_context, WINDOW_STEP
    now = datetime(2026, 9, 30, 13, tzinfo=timezone.utc)

    def window(count):
        rows = [dict(letter_id=f'x{i}', channel='qq', binding_id='b', delivery_status='DELIVERED',
                     created_at=float(i), content='用户说了一段不算短的话' * 6, reply_text='她也回了一段话' * 6)
                for i in range(count)]
        token = READ_WINDOW.set(rows)
        try:
            recent, _ = conversation_context([], query='q', now=now)
        finally:
            READ_WINDOW.reset(token)
        return [item['source_id'] for item in json.loads(recent)['letters']]

    starts = []
    for count in range(30, 38):
        shown = window(count)
        assert shown[-1] == f'reply:x{count - 1}:1'
        starts.append(int(shown[0].split(':')[1][1:]))
    assert all(start % WINDOW_STEP == 0 for start in starts)
    assert len(set(starts)) < len(starts)  # the same start serves several turns


def test_plain_qq_chat_uses_speech_default_with_concrete_text_exceptions():
    from types import SimpleNamespace
    from runtime.reply.companion_runtime import project_decision, media_locked
    from tests.persona.test_jev_pipeline import plan as chat_plan
    plan = chat_plan()
    decision = SimpleNamespace(plan=plan, writer_projection=lambda: {})
    messages = ({'role': 'system', 'content': 'p'}, {'role': 'user', 'content': '晚安'})
    note = project_decision(messages, decision, max_input_chars=10000, delivery='voice_default')[1]['content']
    assert 'QQ本轮默认语音' in note and 'text_reason' in note and '必须是 text' not in note
    assert 'pending_requirements' not in note and '本轮仍需回应' not in note
    assert not media_locked(plan) and media_locked({'understanding': {'requirements': [{'id': 'r1'}]}})
    assert media_locked(None)


def test_oversized_plan_drops_oldest_dialogue_instead_of_failing():
    from types import SimpleNamespace
    from runtime.reply.companion_runtime import project_decision, CompanionRuntimeError
    decision = SimpleNamespace(plan={}, writer_projection=lambda: {'moves': ['x' * 200]})
    dialogue = [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': f'[历史消息 {{}}]\n第{i}句' + '话' * 300}
                for i in range(6)]
    messages = ({'role': 'system', 'content': 'p' * 500}, *dialogue, {'role': 'user', 'content': '在吗'})
    budget = sum(len(m['content']) for m in messages) + 300
    result = project_decision(messages, decision, max_input_chars=budget, delivery='text')
    assert sum(len(m['content']) for m in result) <= budget
    assert all('pending_requirements' not in m['content'] and '本轮仍需回应' not in m['content'] for m in result)
    assert result[-1] == messages[-1] and result[0] == messages[0]
    kept = [m['content'] for m in result if m['content'].startswith('[历史消息 ')]
    assert kept and kept[-1] == dialogue[-1]['content'] and len(kept) < len(dialogue)
    with pytest.raises(CompanionRuntimeError):
        project_decision(({'role': 'system', 'content': 'p' * 500}, {'role': 'user', 'content': '在吗'}),
                         decision, max_input_chars=100, delivery='text')


def test_plan_never_discards_current_user_text_that_looks_like_history():
    from types import SimpleNamespace
    from runtime.reply.companion_runtime import project_decision, CompanionRuntimeError
    decision = SimpleNamespace(plan={}, writer_projection=lambda: {})
    current = {'role': 'user', 'content': '[历史消息 {}]\n' + '当前原文' * 1000}
    with pytest.raises(CompanionRuntimeError, match='JEV_CONTEXT_BUDGET_EXCEEDED'):
        project_decision(({'role': 'system', 'content': '核心规则'}, current),
                         decision, max_input_chars=2000, delivery='text')
