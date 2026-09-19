"""The evidence-use contract is shared and cannot be cropped as optional history."""
import json
import re
from datetime import datetime, timezone

import pytest

from persona_assembly import UntrustedFragment, assemble_persona
from persona_loader import load_persona
from reply_context import ReplyContext, ReplyMode, TrustedTime


@pytest.mark.parametrize('mode', list(ReplyMode))
@pytest.mark.parametrize('world_available', [True, False])
def test_all_formats_keep_evidence_decisions_even_when_history_is_cropped(mode, world_available):
    snapshot = load_persona('linli_character/persona_release_v2.json').snapshot
    context = ReplyContext.create(mode, trusted_time=TrustedTime(datetime(2026, 9, 16, tzinfo=timezone.utc)),
                                  future_im_enabled=mode is ReplyMode.FUTURE_IM,
                                  world_state_available=world_available)
    kwargs = dict(snapshot=snapshot, context=context, user_input='今天过得怎样',
                  history=(UntrustedFragment('synthetic.old', '过去的交流' * 500),))
    full = assemble_persona(**kwargs, max_units=100000)
    tight = assemble_persona(**kwargs, max_units=full.budget_report.required_units)
    assert 'history.synthetic.old' in tight.budget_report.dropped_ids
    protocol = []
    for result in (full, tight):
        assert 'evidence_use' in result.budget_report.included_ids
        matches = re.findall(r'<evidence_use>\s*(.*?)\s*</evidence_use>', result.system_content, re.S)
        assert len(matches) == 1
        protocol.append(json.loads(matches[0]))
    assert protocol[0] == protocol[1]
    # Every form retains all three confidence states and the separate time/scope
    # decision after optional history is dropped; missing data never creates facts.
    assert set(protocol[0]['assertion_status']) == {'confirmed', 'inferred', 'uncertain'}
    assert set(protocol[0]['checks']) == {'source_and_speaker', 'event_stage', 'time_scope', 'conflict'}
    assert protocol[0]['historical_source_is_current_state'] is False
    assert protocol[0]['retrieval_is_confirmation'] is False


def test_historical_reply_is_not_repeated_without_its_source_or_paired_user():
    from runtime.memory.memory_port import CONVERSATION_MEMORY, MemoryRecord, NullMemoryPort
    from runtime.memory.memory_prompt import MemoryPromptBuilder
    from runtime.memory.recall import RecallResult
    from runtime.reply.reply_pipeline import _selected_history

    records = tuple(MemoryRecord(
        memory_id='synthetic:' + speaker, domain=CONVERSATION_MEMORY, text=text,
        source='original_text', created_at=0,
        provenance={'source_record_id': 'history:synthetic', 'speaker': speaker},
        metadata={'complete_original': True, 'canonical': True, 'history_actor': speaker},
    ) for speaker, text in [('user', '我们已经看完展览了。'),
                            ('linli', '你把过程写得像真的一样，我说的是明天打算去。')])
    builder = MemoryPromptBuilder(NullMemoryPort(), conversation_memory=None, max_tokens=300000)
    memory = builder.render(RecallResult(records), max_chars=20000)
    selected = _selected_history(memory)
    combined = '\n'.join(fragment.text for fragment in selected.fragments)
    assert combined.count(records[1].text) == 1
    assert records[0].text in combined
    assert 'character_reply: ' not in combined
    assert len(selected.trusted_evidence.character_replies) == 1
    assert selected.trusted_evidence.character_replies[0].text == records[1].text
