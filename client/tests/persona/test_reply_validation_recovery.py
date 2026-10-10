"""Formatting tolerance must not grant control authority or re-buy a decision."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.reply import reply_model_quality as quality
from runtime.reply.companion_runtime import prepare_decision, CompanionRuntimeError, delivery_for
from runtime.reply.reply_context import ReplyMode
from runtime.reply.reply_reviewer import ReviewVerdict
from reply_orchestrator import ReplyState
from tests.persona.test_jev_pipeline import Port, plan
from tests.persona.test_reply_semantic_wiring import envelope, execute, Reviewer


@pytest.mark.parametrize('wrapper', ['object', 'array', 'fenced'])
def test_rewrite_envelope_recovers_body_without_changing_original_controls(monkeypatch, wrapper):
    original = envelope()
    original.update(followup_at='2026-09-26T15:00:00+08:00', evidence='三点联系我')
    rewritten = {**envelope(), 'text': '好，三点再聊。', 'delivery': 'text',
                 'initiative': 'pause', 'skip': True, 'followup_at': None}
    raw = json.dumps([rewritten] if wrapper == 'array' else rewritten, ensure_ascii=False)
    if wrapper == 'fenced':
        raw = '```json\n' + raw + '\n```'
    monkeypatch.setattr(quality, '_complete_text', lambda *args, **kwargs: raw)
    rewriter = quality.GatewayPersonaRewriter(SimpleNamespace(), Path('unused-persona'), 2)
    reviewer = Reviewer(ReviewVerdict.REWRITE, ReviewVerdict.PASS)
    result, _ = execute(json.dumps(original), mode=ReplyMode.FUTURE_IM,
                        reviewer=reviewer, rewriter=rewriter, raw='三点联系我')
    assert result.state is ReplyState.COMPLETED
    assert json.loads(result.text) == {**original, 'text': rewritten['text']}
    assert reviewer.seen[-1][0] == rewritten['text']
    assert result.reviewer_calls == 2 and result.rewrite_calls == 1


def history(text):
    source = 'reply:prior:1'
    metadata = dict(source=source, event_id=source + ':user', actor='user',
                    evidence_kind='statement_only', truncated=False)
    return (dict(role='user', content='[历史消息 ' + json.dumps(metadata) + ']\n' + text),
            dict(role='user', content='当前合成消息'))


ARGS = dict(source_id='reply:current:user', input_revision=0,
            as_of='2026-10-09T16:45:17+00:00', kinds=['text', 'image'])


def test_serialized_decision_reuses_original_input_when_history_window_changes():
    async def scenario():
        port = Port()
        old = await prepare_decision(port, history('原来的历史'), '当前合成消息', **ARGS)
        saved = json.loads(json.dumps(old.record()))
        replay = await prepare_decision(port, history('重试时历史窗口变化'), '当前合成消息',
                                        cached=saved, **ARGS)
        assert replay.record() == saved and len(port.turns) == 1
        assert saved['input'] == port.turns[0].input
        assert 'input' not in replay.writer_projection()
    asyncio.run(scenario())


@pytest.mark.parametrize('legacy', [False, True])
def test_pipeline_retries_a_failed_writer_with_persisted_decision(legacy):
    from datetime import datetime, timezone
    from reply_orchestrator import ReplyRequest, ReplyResult
    from runtime.personal_chat.presentation import CURRENT
    from runtime.reply.reply_context import ReplyContext, TrustedTime
    from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
    from runtime.reply.reply_reviewer import NullReviewer

    async def scenario():
        saved, writer_calls = {}, []
        class Engine:
            async def run(self, request):
                writer_calls.append(request)
                return ReplyResult(request.request_id, ReplyState.COMPLETED,
                    text='{"text":' if len(writer_calls) == 1 else json.dumps(envelope()))
        async def persist(record):
            saved.update(json.loads(json.dumps(record)))
        port = Port()
        pipeline = ReplyPipeline(Engine(), reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
            companion_decision_port=port, discover_runtime_ports=False)
        context = ReplyContext.create(ReplyMode.FUTURE_IM, future_im_enabled=True,
            trusted_time=TrustedTime(datetime(2026, 10, 9, 16, 45, tzinfo=timezone.utc)))
        results = []
        for attempt in (1, 2):
            if legacy:
                saved.pop('input', None)
            token = CURRENT.set(dict(structured=True, raw_user_text='当前合成消息', channel='qq',
                received_source_id=ARGS['source_id'], input_revision=0, semantic_kinds=['text', 'audio_speech'],
                decision_now=context.trusted_time.instant.isoformat(),
                companion_decision=dict(saved) if saved else None, save_companion_decision=persist))
            try:
                results.append(await pipeline.run(ReplyRequest(request_id=f'synthetic:{attempt}',
                    content='当前合成消息', messages=history('旧窗口' if attempt == 1 else '新窗口'),
                    max_input_chars=40000), context))
            finally:
                CURRENT.reset(token)
        assert results[0].error_code == 'PERSONAL_CHAT_DECISION_INVALID'
        assert results[0].decision_rejection_reason == 'JSON_SYNTAX'
        assert len(port.turns) == 1
        if legacy:
            assert results[1].error_code == 'JEV_STORED_DECISION_INVALID' and len(writer_calls) == 1
        else:
            assert results[1].state is ReplyState.COMPLETED, (results[1].error_code, results[1].decision_rejection_reason)
            assert len(writer_calls) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize('mutation', ['snapshot', 'source', 'current_text', 'digest', 'snapshot_shape'])
def test_snapshot_replay_rejects_tampering_without_another_provider_call(mutation):
    async def scenario():
        port = Port()
        old = await prepare_decision(port, history('原来的历史'), '当前合成消息', **ARGS)
        saved = deepcopy(old.record())
        args, user = dict(ARGS), '当前合成消息'
        if mutation == 'snapshot':
            saved['input']['messages'][0]['text'] = '已被修改'
        elif mutation == 'snapshot_shape':
            saved['input']['current_turn_id'] = 't999'
        elif mutation == 'source':
            args['source_id'] = 'reply:another:user'
        elif mutation == 'current_text':
            user = '未经版本变更的当前消息'
        else:
            saved['input_digest'] = '0' * 64
        with pytest.raises(CompanionRuntimeError, match='JEV_STORED_DECISION_INVALID'):
            await prepare_decision(port, history('原来的历史'), user, cached=saved, **args)
        assert len(port.turns) == 1
    asyncio.run(scenario())


def test_new_input_revision_gets_a_new_decision_and_old_snapshot_stays_unchanged():
    async def scenario():
        port = Port()
        old = await prepare_decision(port, (), '第一条', **ARGS)
        saved = deepcopy(old.record())
        new = await prepare_decision(port, (), '合并后的新消息', cached=saved, **{**ARGS, 'input_revision': 1})
        assert len(port.turns) == 2 and new.input_revision == 1
        assert old.record() == saved
    asyncio.run(scenario())


def test_legacy_decision_without_snapshot_still_checks_rebuilt_context():
    async def scenario():
        port = Port()
        old = await prepare_decision(port, history('原来的历史'), '当前合成消息', **ARGS)
        saved = old.record()
        saved.pop('input', None)
        replay = await prepare_decision(port, history('原来的历史'), '当前合成消息', cached=saved, **ARGS)
        assert replay.record() == saved
        with pytest.raises(CompanionRuntimeError, match='JEV_STORED_DECISION_INVALID'):
            await prepare_decision(port, history('不同历史'), '当前合成消息', cached=saved, **ARGS)
        assert len(port.turns) == 1
    asyncio.run(scenario())


def test_frozen_decision_does_not_restore_a_removed_media_capability():
    async def scenario():
        port = Port(plan(kind='image'))
        old = await prepare_decision(port, (), '合成图片请求', **ARGS)
        replay = await prepare_decision(port, (), '合成图片请求', cached=old.record(), **{**ARGS, 'kinds': ['text']})
        with pytest.raises(CompanionRuntimeError, match='JEV_PLAN_UNSUPPORTED'):
            delivery_for(replay, kinds=['text'])
        assert len(port.turns) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('judgment,expected', [('uncertain', 'pass'), ('earlier', 'rewrite')])
def test_only_clear_off_turn_judgment_requires_rewrite(monkeypatch, judgment, expected):
    from tests.persona.test_jev_quality import Decisions, transport, request
    class OffTurn(Decisions):
        async def ask(self, state, questions, **kwargs):
            answers = await super().ask(state, questions, **kwargs)
            for key, question in questions.items():
                if key.endswith(':STAGE_DRIFT'):
                    answers[key] = 'none'
                if key.endswith(':OFF_TURN_REPLY'):
                    assert judgment in question['criteria']
                    answers[key] = judgment
            return answers
    port = OffTurn()
    result = transport(monkeypatch, port).review_json(request('合成回复。'), model='jev', timeout_seconds=5)
    assert result['verdict'] == expected
    assert [item['code'] for item in result['violations']] == ([] if expected == 'pass' else ['OFF_TURN_REPLY'])
    assert len(port.calls) == 1
