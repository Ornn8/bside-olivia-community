from pathlib import Path
from types import SimpleNamespace
import asyncio
from datetime import datetime, timezone
import json
import pytest

from runtime.memory.memory_port import CONVERSATION_MEMORY, MemoryRecord, NullMemoryPort
from runtime.memory.memory_prompt import MemoryPrompt, MemoryPromptBuilder


def original(source, actor, text, topic):
    return MemoryRecord(memory_id=source + ':' + actor, domain=CONVERSATION_MEMORY,
        text=text, source='original_text', created_at=0,
        provenance={'source_record_id': source, 'speaker': actor, 'verbatim': True},
        metadata={'complete_original': True, 'verbatim': True, 'topic_indexes': str(topic)})


def test_render_reports_final_coverage_and_retains_frozen_candidates():
    from runtime.memory.recall import RecallResult
    records = tuple(original(f'history:{i}', actor, topic + ' detail' * 25, i)
        for i, topic in enumerate(('breakfast', 'ring')) for actor in ('user', 'linli'))
    recall = RecallResult(records, topics=('breakfast', 'ring'),
        source_status=(('original_index', 'available'),))
    builder = MemoryPromptBuilder(NullMemoryPort(), conversation_memory=None, max_tokens=300000)
    full = builder.render(recall, max_chars=20000)
    narrow = builder.render(recall, max_chars=1500)
    assert len(full.references) == 4
    assert len(narrow.references) == 2
    assert narrow.recall_result is recall
    assert narrow.truncated
    assert 'omitted' in narrow.text
    assert len(narrow.text) <= 1500
    assert {r.provenance['source_record_id'] for r in narrow.references} in ({'history:0'}, {'history:1'})


def test_no_evidence_failure_is_preserved_in_final_persona():
    from runtime.memory.recall import RecallResult
    from local_server import LetterAdapter
    from llm_gateway import GatewayConfig
    adapter = LetterAdapter(GatewayConfig(provider='mock'), memory_port=NullMemoryPort())
    result = RecallResult((), topics=('戒指',), source_status=(('original_index', 'unavailable'),),
                          stop_reason='partial_source_failure')
    builder = MemoryPromptBuilder(NullMemoryPort(), conversation_memory=None, max_tokens=300000)
    adapter.memory_prompt_builder = SimpleNamespace(build=lambda *a, **k: builder.render(result, max_chars=k['max_chars']))
    messages = adapter._persona_v2_messages('还记得戒指吗')
    assert 'original' in messages[0]['content']
    assert 'unavailable' in messages[0]['content']
    assert 'absence' in messages[0]['content']


def test_tight_budget_keeps_failure_and_omission_state_without_originals():
    from runtime.memory.recall import RecallResult
    records = (original('history:large', 'user', '原文内容' * 1000, 0),)
    recall = RecallResult(records, topics=('戒指',), source_status=(('original_index', 'unavailable'),))
    builder = MemoryPromptBuilder(NullMemoryPort(), conversation_memory=None, max_tokens=300000)
    prompt = builder.render(recall, max_chars=240)
    from runtime.memory.memory_prompt import _unescape_reserved
    assert 'original_index' in _unescape_reserved(prompt.text)
    assert 'omitted' in prompt.text
    assert 'does not prove' in prompt.text
    assert not prompt.references
    assert prompt.truncated
    assert len(prompt.text) <= 240


def test_near_full_persona_still_discloses_retrieval_failure():
    from runtime.memory.recall import RecallResult
    from runtime.reply.reply_pipeline import assemble_reply_messages
    from persona_loader import load_persona
    from reply_context import ReplyContext, ReplyMode, TrustedTime
    snapshot = load_persona('linli_character/persona_release_v2.json').snapshot
    context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=TrustedTime(datetime(2026, 9, 16, tzinfo=timezone.utc)))
    capacity = 10000
    recall = RecallResult(topics=('你好',), source_status=(('original_index', 'unavailable'),))
    renderer = MemoryPromptBuilder(NullMemoryPort(), conversation_memory=None, max_tokens=300000)
    adapter = SimpleNamespace(memory_prompt_builder=SimpleNamespace(
        build=lambda query, max_chars: renderer.render(recall, max_chars=max_chars)))
    messages, _ = assemble_reply_messages(adapter, snapshot, context, '你好', max_input_chars=capacity)
    from runtime.reply.reply_model_quality import _assembled_memory_evidence
    evidence = _assembled_memory_evidence(messages)
    assert 'original' in evidence and 'index' in evidence
    assert 'unavailable' in evidence
    assert sum(len(m['content']) for m in messages) <= capacity


def test_unplaceable_recall_state_fails_before_generation():
    from runtime.memory.recall import RecallResult
    from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
    from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
    from runtime.reply.reply_reviewer import NullReviewer
    from reply_orchestrator import ReplyRequest, ReplyState
    from persona_loader import load_persona
    renderer = MemoryPromptBuilder(NullMemoryPort(), conversation_memory=None, max_tokens=300000)
    recall = RecallResult(topics=('戒指',), source_status=(('original_index', 'unavailable'),))
    adapter = SimpleNamespace(config=SimpleNamespace(provider='mock', persona_v2_enabled=True),
        persona_v2_path='linli_character/persona_release_v2.json',
        memory_prompt_builder=SimpleNamespace(build=lambda query, max_chars: renderer.render(recall, max_chars=max_chars)))
    from persona_assembly import assemble_persona
    snapshot = load_persona(adapter.persona_v2_path).snapshot
    context = ReplyContext.create(ReplyMode.VOICE_REPLY, trusted_time=TrustedTime(datetime(2026, 9, 16, tzinfo=timezone.utc)))
    baseline = assemble_persona(snapshot, context, user_input='戒指', max_units=100000,
                                relationship_expression_enabled=True)
    capacity = baseline.budget_report.required_units
    class NeverGenerate:
        gateway = SimpleNamespace(adapter=adapter)
        async def run(self, request):
            pytest.fail('generation must not proceed after dropping the retrieval state')
    pipeline = ReplyPipeline(NeverGenerate(), reviewer=NullReviewer(), rewriter=UnavailableRewriter(), discover_runtime_ports=False)
    result = asyncio.run(pipeline.run(ReplyRequest(content='戒指', max_input_chars=capacity), context))
    assert result.state is ReplyState.FAILED
    assert result.error_code == 'RECALL_CONTEXT_BUDGET_EXCEEDED'


def test_repacking_does_not_search_again():
    from runtime.memory.recall import RecallResult
    class Port:
        calls = 0
        def status(self):
            return {'status': 'available'}
        def search(self, query, **kwargs):
            self.calls += 1
            return [original('history:a', 'linli', 'breakfast original', 0)]
    port = Port()
    builder = MemoryPromptBuilder(port, conversation_memory=None, max_tokens=300000)
    first = builder.build('breakfast', max_chars=20000)
    assert isinstance(first.recall_result, RecallResult)
    second = builder.render(first.recall_result, max_chars=1000)
    assert port.calls == 1
    assert second.recall_result is first.recall_result
    assert second.references


def test_real_letter_adapter_world_and_prompt_share_one_recent_snapshot():
    from local_server import LetterAdapter
    from llm_gateway import GatewayConfig
    from persona_assembly import UntrustedFragment
    adapter = LetterAdapter(GatewayConfig(provider='mock'), memory_port=NullMemoryPort())
    reads, world_related = [], []
    def recent(content):
        reads.append(content)
        return (UntrustedFragment('letters.recent', json.dumps({'letters': [
            {'user_letter': f'frozen-recent-{len(reads)}', 'linli_reply': 'recorded response'}
        ]})),)
    def world_context(content, *, now, related_text):
        world_related.append(related_text)
        return json.dumps({'kind': 'character_life_reference', 'related': related_text})
    adapter.recent_letter_fragments = recent
    adapter.daily_life = SimpleNamespace(store=SimpleNamespace(reply_context=world_context),
                                         snapshot=lambda now: {'rhythm': {}})
    adapter._memory_context_limit = lambda: 0
    adapter._build_memory_prompt = lambda *args, **kwargs: MemoryPrompt()
    messages = adapter._persona_v2_messages('你好')
    assert len(reads) == 1
    assert world_related == ['frozen-recent-1\nrecorded response']
    assert 'frozen-recent-1' in messages[0]['content']
    assert 'frozen-recent-2' not in messages[0]['content']


def test_unavailable_is_not_evidence_of_no_history():
    from runtime.memory.recall import RecallResult
    recall = RecallResult((), topics=('ring',), source_status=(('original_index', 'unavailable'),))
    assert recall.coverage(())[0]['state'] == 'unavailable'
    assert RecallResult((), topics=('ring',), source_status=(('original_index', 'available'),)).coverage(())[0]['state'] == 'not_found'


@pytest.mark.parametrize('mode,channel', [
    ('text_letter', None), ('voice_reply', None), ('spoken_video', None),
    ('singing_video', None), ('voice_song_video', None), ('musical_video', None),
    ('future_im', 'qq'), ('future_im', 'wechat')])
def test_event_source_zero_hit_trace_reaches_actual_generation_call(tmp_path, mode, channel):
    from local_server import LetterAdapter
    from llm_gateway import GatewayConfig
    from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter
    from tests.memory.test_mem0_memory import FakeMem0, _config
    from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
    from runtime.reply.reply_reviewer import NullReviewer
    from runtime.reply.reply_context import ReplyMode
    from reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
    from persona_assembly import UntrustedFragment
    from runtime.personal_chat.presentation import CURRENT
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))
    memory._originals.put('local-user', 'reply:event:1', '银环是去年挑的',
        '登记原定明天办理，后来取消了', datetime(2026, 9, 1, tzinfo=timezone.utc))
    adapter = LetterAdapter(GatewayConfig(provider='mock'), memory_port=NullMemoryPort(), conversation_memory=memory)
    reads = []
    def life(content):
        reads.append(content)
        return (UntrustedFragment('linli.daily-life', json.dumps({'threads': [
            {'source_id': 'reply:event:1', 'status': 'cancelled', 'quote': '后来取消了'}
        ]}, ensure_ascii=False)),)
    adapter.daily_life_fragments = life
    selection_calls = []
    async def select_history(messages, **kwargs):
        selection_calls.append(messages)
        candidates = json.loads(messages[1]['content'])['candidates']
        matching = [item['id'] for item in candidates
                    if '银环是去年挑的' in json.dumps(item['records'], ensure_ascii=False)]
        return SimpleNamespace(text=json.dumps({'selected_ids': matching[:1]}))
    adapter.gateway.complete_structured_scoped = select_history
    class Capture:
        gateway = SimpleNamespace(adapter=adapter)
        async def run(self, request):
            self.request = request
            return ReplyResult('test', ReplyState.COMPLETED, text='synthetic response')
    capture = Capture()
    token = CURRENT.set({'channel': channel, 'structured': True}) if channel else None
    try:
        context = adapter.build_reply_context(ReplyMode(mode), future_im_enabled=True)
        pipeline = ReplyPipeline(capture, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                                 discover_runtime_ports=False)
        result = asyncio.run(pipeline.run(ReplyRequest(content='你记不记得那件事情'), context))
    finally:
        if token is not None:
            CURRENT.reset(token)
    assert result.state is ReplyState.COMPLETED
    assert result.quality_status == 'not_checked'
    assert len(selection_calls) == 1
    system = capture.request.messages[0]['content']
    assert '银环是去年挑的' in system
    assert '登记原定明天办理，后来取消了' in system
    assert '2026-09-01' in system
    assert 'cancelled' in system
    assert len(reads) == 1
    assert len(selection_calls) == 1
    assert sum(len(m['content']) for m in capture.request.messages) <= capture.request.max_input_chars
def test_topics_preserve_distinct_clauses_and_deduplicate_before_numbering():
    from runtime.memory.recall import query_topics
    assert query_topics('戒指还在手上，民政局明天去；早餐吃培根。戒指还在手上') == (
        '戒指还在手上', '民政局明天去', '早餐吃培根')
