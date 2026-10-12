import asyncio
import pytest

from runtime.personal_chat.presentation import CURRENT
from runtime.reply.companion_decision import CompanionDecisionResult, FrozenCompanionDecision, MODEL
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_orchestrator import ReplyRequest, ReplyState
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer
from tests.http.test_personal_chat_decision import envelope
from tests.persona.test_companion_decision import project
from tests.persona.test_jev_pipeline import plan
from tests.persona.test_qq_recovery_pipeline import NOW
from tests.persona.test_reply_semantic_wiring import Engine
from tests.media.test_piano import CAPS


@pytest.mark.parametrize('timing,enabled,expected', [('now', True, True), ('defer', True, False), ('now', False, False)])
def test_actual_writer_gets_only_executable_piano_instruction(timing, enabled, expected):
    class Port:
        async def decide(self, turn):
            value = plan(timing=timing)
            response = dict(schema_version='companion-shadow/1', backend='jev', model=MODEL,
                contract_valid=True, status='valid_contract', fallback=False, action_executed=False,
                production_approved=False, latency_ms=1, plan=value, tasks=project(value),
                api_calls=1, usage={'input_tokens': 1}, piano_request=dict(source='original', piece_hint='夜曲', seconds=30))
            return CompanionDecisionResult(decision=FrozenCompanionDecision.from_response(turn, response))
    engine = Engine(envelope(text='等我一下，去琴房录给你。', delivery='text'))
    pipeline = ReplyPipeline(engine, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
        discover_runtime_ports=False, companion_decision_port=Port())
    context = ReplyContext.create(ReplyMode.FUTURE_IM, future_im_enabled=True, trusted_time=TrustedTime(NOW))
    metadata = dict(structured=True, proactive=False, channel='qq', raw_user_text='弹一段夜曲给我听',
        semantic_kinds=['text'], received_source_id='synthetic-piano', input_revision=0,
        decision_now=NOW.isoformat(), piano_experience=CAPS if enabled else None)
    token = CURRENT.set(metadata)
    try:
        result = asyncio.run(pipeline.run(ReplyRequest(content=metadata['raw_user_text'], request_id='synthetic-piano',
            messages=({'role': 'user', 'content': metadata['raw_user_text']},)), context))
    finally:
        CURRENT.reset(token)
    assert result.state is ReplyState.COMPLETED
    notes = '\n'.join(message['content'] for message in engine.requests[0].messages)
    assert ('<piano_task>' in notes) is expected
    if expected:
        assert '不声称已经录好' in notes and '夜曲' in notes and '一整首' in notes
