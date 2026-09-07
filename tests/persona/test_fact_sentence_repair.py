import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.reply import reply_model_quality as quality
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_reviewer import ReviewerViolation


def test_fact_repair_preserves_unaffected_affection_and_context(monkeypatch):
    candidate = '喜欢和你聊天。我的爱好有看电影、每周去码头。下回给你讲电影。'
    start = candidate.index('每周')
    original = ({'role': 'system', 'content': 'Frozen context'},
                {'role': 'user', 'content': '聊聊爱好吧。'})
    def complete(gateway, messages, *args, **kwargs):
        assert tuple(messages[1:-1]) == original
        payload = json.loads(messages[-1]['content'])
        assert payload['editable_sentences'] == [
            {'id': '0', 'text': '我的爱好有看电影、每周去码头。'}]
        return json.dumps({'edits': [{'id': '0', 'replacement': '我的爱好有看电影。'}]})
    monkeypatch.setattr(quality, '_complete_text', complete)
    rewriter = quality.GatewayPersonaRewriter(SimpleNamespace(), Path('unused'), 2)
    reply = rewriter.rewrite_with_evidence(
        candidate, ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=TrustedTime(datetime(2026, 1, 1, tzinfo=timezone.utc))),
        ('MEMORY_FABRICATION',), original,
        (ReviewerViolation('MEMORY_FABRICATION', 'hard', start, start + 5),))
    assert reply == '喜欢和你聊天。我的爱好有看电影。下回给你讲电影。'


def test_fact_windows_merge_overlapping_claims_keep_conditions_and_newlines():
    candidate = '开头。\n如果有空，我每周去码头，还会游泳。\n结尾。'
    evidence = [{'start': candidate.index('每周'), 'end': candidate.index('码头') + 2},
                {'start': candidate.index('还会'), 'end': candidate.index('游泳') + 2}]
    spans = quality._fact_repair_sentences(candidate, evidence)
    assert [candidate[start:end] for start, end in spans] == ['如果有空，我每周去码头，还会游泳。']


@pytest.mark.parametrize('edits', [[], [{'id': '1', 'replacement': '其他句子'}],
    [{'id': '0', 'replacement': ''}, {'id': '0', 'replacement': '重复'}],
    [{'id': '0', 'replacement': None}], [{'id': '0', 'replacement': '好。', 'extra': 1}]])
def test_fact_edit_contract_rejects_missing_duplicate_or_unknown_regions(edits):
    with pytest.raises((ValueError, RuntimeError)):
        quality._apply_fact_sentence_edits('前文。错误。后文。', [(3, 6)], json.dumps({'edits': edits}))
