"""Optional selected world data must not disable an otherwise valid text reply."""
import asyncio
import json

import pytest

from persona_assembly import UntrustedFragment
from reply_orchestrator import ReplyRequest, ReplyState
from runtime.reply.reply_context import ReplyMode
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import ReviewVerdict
from tests.http.test_personal_chat_decision import envelope
from tests.persona.test_jev_reply_recovery import invoke
from tests.persona.test_reply_pipeline import ROOT, _configured_v2_pipeline
from tests.persona.test_reply_semantic_wiring import Engine, Reviewer


@pytest.mark.parametrize('mode,budget,proactive', [
    (ReplyMode.TEXT_LETTER, 16000, False), (ReplyMode.FUTURE_IM, 21000, False),
    (ReplyMode.FUTURE_IM, 21000, True)])
def test_world_omission_respects_received_text_recovery_scope(monkeypatch, mode, budget, proactive):
    monkeypatch.setenv('OLIVIA_REPLY_REVIEW_ENABLED', 'false')
    monkeypatch.setenv('OLIVIA_LETTER_CURRENT_TURN_INTERPRETATION', '0')
    monkeypatch.delenv('OLIVIA_JEV_DECISION_URL', raising=False)
    _, _, bridge, _ = _configured_v2_pipeline(ROOT / 'linli_character/persona_release_v2.json')
    world = dict(kind='character_life_reference', stale=False,
                 current=dict(activity='读书', note='合成已选世界事实。' * 250))
    fragments = (UntrustedFragment('linli.daily-life', json.dumps(world, ensure_ascii=False)),)
    calls = []

    async def select(*args, **kwargs):
        calls.append('selection')
        return fragments

    monkeypatch.setattr(bridge.adapter, 'daily_life', object())
    monkeypatch.setattr(bridge.adapter, 'prepare_daily_life_fragments', select)
    monkeypatch.setattr(bridge.adapter, 'daily_life_fragments',
                        lambda *a, **k: pytest.fail('Do not reload world or reselect after omission'))
    engine = Engine(envelope(delivery='text', sticker=None) if mode is ReplyMode.FUTURE_IM else '收到啦。')
    engine.gateway = bridge
    reviewer = Reviewer(ReviewVerdict.PASS)
    pipeline = ReplyPipeline(engine, reviewer=reviewer, rewriter=UnavailableRewriter(),
                             discover_runtime_ports=False)
    raw = '你好，今天怎么样？'
    result = asyncio.run(invoke(pipeline, mode, raw,
        metadata={'proactive': proactive},
        request=ReplyRequest(content=raw, request_id='synthetic-world-budget', max_input_chars=budget)))
    if proactive:
        assert result.state is ReplyState.FAILED and result.error_code == 'JEV_WORLD_SELECTION_BUDGET'
        assert not engine.requests and not result.degraded_stages
        return
    assert result.state is ReplyState.COMPLETED, result.error_code
    assert result.degraded_stages == {'world': 'JEV_WORLD_SELECTION_BUDGET'}
    assert calls == ['selection'] and len(engine.requests) == len(reviewer.seen) == 1
    assert result.companion_delivery == 'text' and result.companion_decision is None
    assert result.expression_context['world_used'] is False
    wire = '\n'.join(m['content'] for m in engine.requests[0].messages)
    assert '合成已选世界事实' not in wire and 'constitution' in wire
    assert raw in wire and '缺失信息保持未知' in wire
    assert len(wire) <= budget
