"""Reconsidering a silent proposal cannot execute pending long speech."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from runtime.personal_chat import backend, speech
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.presentation import CURRENT
from runtime.reply.companion_decision import CompanionDecisionResult, FrozenCompanionDecision, MODEL
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_orchestrator import ReplyRequest, ReplyState
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer
from tests.http.test_chat_jev_decision import server_fixture
from tests.http.test_personal_chat_decision import envelope
from tests.persona.test_companion_decision import project
from tests.persona.test_jev_pipeline import plan
from tests.persona.test_qq_recovery_pipeline import NOW
from tests.persona.test_reply_semantic_wiring import Engine


USER = '后天再继续讲故事，今天先聊聊。'
INTENT = dict(mode='story', target_seconds=120, continuation=True)
SCRIPT = dict(title='后续故事', spoken_text='这是合成测试中的故事正文。' * 5, continuation_summary='合成故事未曾发送。')


class PendingSpeechPort:
    async def decide(self, turn):
        value = plan(timing='defer')
        value['understanding']['requirements'] = [dict(id='later_story', fulfillment='pending',
            alternatives=[dict(kinds=['audio_speech'], min_assets=1, max_assets=1)], evidence_turn_ids=['t1'])]
        response = dict(schema_version='companion-shadow/1', backend='jev', model=MODEL,
            contract_valid=True, status='valid_contract', fallback=False, action_executed=False,
            production_approved=False, latency_ms=1, plan=value, tasks=project(value),
            api_calls=1, usage={'input_tokens': 1}, speech_request=deepcopy(INTENT))
        return CompanionDecisionResult(decision=FrozenCompanionDecision.from_response(turn, response))


def run_pending_speech(body):
    engine = Engine(body)
    pipeline = ReplyPipeline(engine, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
        discover_runtime_ports=False, companion_decision_port=PendingSpeechPort())
    context = ReplyContext.create(ReplyMode.FUTURE_IM, future_im_enabled=True, trusted_time=TrustedTime(NOW))
    metadata = dict(structured=True, proactive=False, channel='qq', raw_user_text=USER,
        semantic_kinds=['text', 'audio_speech'], received_source_id='synthetic-pending-speech',
        input_revision=0, decision_now=NOW.isoformat(), speech_enabled=True)
    token = CURRENT.set(metadata)
    try:
        result = asyncio.run(pipeline.run(ReplyRequest(content=USER, request_id='synthetic-pending-speech',
            messages=({'role': 'user', 'content': USER},)), context))
        return result, engine
    finally:
        CURRENT.reset(token)


def test_reconsidered_silence_keeps_pending_intent_without_authoring_long_speech():
    result, engine = run_pending_speech(envelope(text='好，今天先聊聊。', delivery='text'))
    assert result.state is ReplyState.COMPLETED and len(engine.requests) == 1
    assert result.companion_decision['speech_request'] == INTENT
    assert result.companion_decision['plan']['understanding']['requirements'][0]['fulfillment'] == 'pending'
    assert result.companion_decision['plan']['proposal']['steps'] == []
    assert not any('<speech_request>' in message['content'] for message in engine.requests[0].messages)


def test_speech_body_cannot_gain_execution_from_reconsidered_silent_plan():
    result, _ = run_pending_speech(envelope(text='好，今天先聊聊。', delivery='text', speech=SCRIPT))
    assert result.state is ReplyState.FAILED
    assert result.error_code == 'PERSONAL_CHAT_DECISION_INVALID'
    assert result.decision_rejection_reason == 'MEDIA_WITHOUT_PLAN'


def test_backend_revalidates_pending_speech_before_queuing_file(monkeypatch, tmp_path):
    normal, _ = run_pending_speech(envelope(text='好，今天先聊聊。', delivery='text'))
    injected = SimpleNamespace(state=ReplyState.COMPLETED, error_code=None,
        text=envelope(text='好，今天先聊聊。', delivery='text', speech=SCRIPT),
        companion_decision=normal.companion_decision, companion_timing='now', companion_delivery='text')
    server, row, *_ = server_fixture(monkeypatch, tmp_path, injected)
    async def supported(environment): return True
    monkeypatch.setattr(speech, 'supported', supported)
    with pytest.raises((ValueError, RuntimeError), match='PERSONAL_CHAT_DECISION_INVALID|JEV_PLAN_UNSUPPORTED'):
        asyncio.run(backend.generate(server, PersonalMessage('qq', 'b', 'u', 'pending', USER), row))
    assert 'speech_script' not in row and 'speech_status' not in row
