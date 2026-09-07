import json
from dataclasses import replace
from pathlib import Path

import pytest

from runtime.persona.persona_loader import load_persona
from runtime.reply import reply_model_quality as quality


def adjudicate(monkeypatch, results, candidate, decide):
    def complete(gateway, messages, timeout, **kwargs):
        payload = json.loads(messages[-1]['content'])
        claims = payload['claims']
        assert len({c['evidence_id'] for c in claims}) == len(claims)
        return json.dumps({'decisions': [dict(
            evidence_id=c['evidence_id'], code=c['code'], start=c['start'], end=c['end'],
            decision=decide(c, payload),
        ) for c in claims]})
    monkeypatch.setattr(quality, '_complete_text', complete)
    persona = Path(__file__).resolve().parents[2] / 'linli_character/persona_release_v2.json'
    return quality._adjudicate_hard_evidence(None, results,
        authorities=quality._build_release_layer_authorities(load_persona(persona).snapshot, mode='text_letter'),
        candidate=candidate, current_user_input='早上好。', character_reply_history='',
        memory_evidence={}, relationship_context={}, timeout_seconds=1, gateway_scope=None)


@pytest.mark.parametrize('local_id', ['evt_001', 'e' * 96])
def test_independent_layers_can_reuse_local_ids_without_mixing_decisions(monkeypatch, local_id):
    candidate = '我每天都去码头。今天挺开心。'
    boundary = candidate.index('今天')
    fact = quality._HardReviewEvidence(local_id, 'MEMORY_FABRICATION', 0, boundary,
                                      'habit', 'none', 'INVENTED_HABIT')
    style = replace(fact, code='STYLE_DRIFT', start=boundary, end=len(candidate),
                    claim_kind='generic_assistant_tone', reason_code='GENERIC_TONE')
    results = (quality._LayerResult('voice_style', 1, ('STYLE_DRIFT',), True, hard_evidence=(style,)),
               quality._LayerResult('continuity_memory', 1, ('MEMORY_FABRICATION',), False, hard_evidence=(fact,)))
    def decide(claim, payload):
        assert claim['quote'] == candidate[claim['start']:claim['end']]
        assert claim['context_id'] == quality._adjudication_context_id(claim['layer'], claim['code'])
        return 'CONFIRM' if claim['layer'] == 'continuity_memory' else 'REJECT'
    outcome = adjudicate(monkeypatch, results, candidate, decide)
    assert outcome.results[0].hard_evidence == ()
    assert outcome.results[0].rejected_evidence == (style,)
    assert outcome.results[1].hard_evidence == (fact,)
    assert len(outcome.confirmed_evidence) == 1
    assert outcome.confirmed_evidence[0].code == 'MEMORY_FABRICATION'


def test_duplicate_ids_inside_one_layer_are_still_rejected(monkeypatch):
    evidence = quality._HardReviewEvidence('same', 'MEMORY_FABRICATION', 0, 1,
                                          'habit', 'none', 'INVENTED_HABIT')
    result = quality._LayerResult('continuity_memory', 1, ('MEMORY_FABRICATION',), False,
                                  hard_evidence=(evidence, replace(evidence, start=1, end=2)))
    with pytest.raises(RuntimeError, match='ADJUDICATION_EVIDENCE_DUPLICATE'):
        adjudicate(monkeypatch, (result,), '甲乙', lambda *_: pytest.fail('must reject before calling gateway'))
