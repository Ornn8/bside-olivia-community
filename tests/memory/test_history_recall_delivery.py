"""Regression tests for evidence surviving the final generation boundary."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
from types import SimpleNamespace

import pytest

from runtime.memory.recall_check import prepare_recall_messages


def messages():
    groups = [
        [{'citation': 'trip:user', 'source_id': 'history:trip', 'speaker': 'user',
          'text': '我们计划周五去海边。'},
         {'citation': 'trip:linli', 'source_id': 'history:trip', 'speaker': 'linli',
          'text': '后来取消了，没有去成。'}],
        [{'citation': 'scarf:user', 'source_id': 'history:scarf', 'speaker': 'user',
          'text': '外婆的围巾上缝着一颗铜纽扣。'},
         {'citation': 'scarf:linli', 'source_id': 'history:scarf', 'speaker': 'linli',
          'text': '那颗铜纽扣我记住了。'}],
    ]
    raw = '[ORIGINAL_CORRESPONDENCE_UNTRUSTED]\n' + '\n'.join(json.dumps(g, ensure_ascii=False) for g in groups)
    system = '<evidence_use>{}</evidence_use><untrusted_history>' + json.dumps({'text': raw}, ensure_ascii=False) + '</untrusted_history>'
    return [{'role': 'system', 'content': system},
            {'role': 'user', 'content': '海边去了没有？围巾上缝着什么？'}]


class LiveCheck:
    config = SimpleNamespace(provider='openai_compatible')
    def __init__(self, *, partial=False, timeout=False):
        self.partial, self.timeout, self.calls = partial, timeout, []

    async def complete_scoped(self, prompt, **kwargs):
        self.calls.append(deepcopy(prompt))
        if self.timeout:
            raise TimeoutError()
        findings = [{'topic': '海边', 'status': 'confirmed', 'event_stage': 'cancelled',
                     'finding': '计划后来取消，没有去成。',
                     'citations': [{'source': 's0', 'quote': '后来取消了，没有去成。'}]}]
        if self.partial:
            findings.append({'topic': '围巾', 'status': 'confirmed', 'event_stage': 'completed',
                             'finding': '围巾上有纽扣。',
                             'citations': [{'source': 's1', 'quote': '这句原文并不存在'}]})
        return SimpleNamespace(text=json.dumps({'reply_intent': 'recall_question',
            'direct_questions': ['海边去了没有？', '围巾上缝着什么？'], 'findings': findings}, ensure_ascii=False))


@pytest.mark.parametrize('partial', [False, True])
def test_unquoted_topic_keeps_both_original_sides_after_check(partial):
    original = messages()
    before = deepcopy(original)
    gateway = LiveCheck(partial=partial)
    final = asyncio.run(prepare_recall_messages(original, gateway, max_input_chars=20000))
    assert original == before
    assert final[0]['content'].startswith(before[0]['content'])
    assert '外婆的围巾上缝着一颗铜纽扣。' in final[0]['content']
    assert '那颗铜纽扣我记住了。' in final[0]['content']
    assert '我们计划周五去海边。' in final[0]['content']
    assert '后来取消了，没有去成。' in final[0]['content']
    assert len(gateway.calls) == 1


def test_timeout_retains_originals_and_does_not_claim_no_history():
    original = messages()
    final = asyncio.run(prepare_recall_messages(original, LiveCheck(timeout=True), max_input_chars=20000))
    assert final[0]['content'].startswith(original[0]['content'])
    assert 'unavailable' in final[0]['content']


def query_fragment(text, *, source='reply:previous:1', name='letters.recent'):
    from persona_assembly import UntrustedFragment
    return UntrustedFragment(name, json.dumps({'letters': [{'source_id': source,
        'time': '2026-09-16T08:00:00+00:00', 'user_letter': text,
        'linli_reply': '我们可以接着聊。'}]}, ensure_ascii=False))


def test_deictic_query_uses_only_delivered_user_context():
    from runtime.memory.history_continuity import plan_history_query
    recent = (query_fragment('聊聊外婆留下的围巾。'),)
    plan = plan_history_query('你记得上面缝着什么吗？', recent)
    assert plan.mode == 'contextual'
    assert '外婆留下的围巾' in plan.query
    assert '铜纽扣' not in plan.query  # The planner cannot invent the answer.
    assert '我们可以接着聊' not in plan.query
    assert plan_history_query('那件事你还记得吗？').mode == 'ambiguous'
    assert plan_history_query('围巾上是什么纽扣？', recent).mode == 'direct'


@pytest.mark.parametrize('recent', [
    (query_fragment('不能泄漏的旧话题', name='chat.historical'),),
    (query_fragment('不能泄漏的旧话题', source='reply:excluded:1'),),
])
def test_historical_candidates_or_excluded_turns_cannot_supply_the_anchor(recent):
    from runtime.memory.history_continuity import plan_history_query
    plan = plan_history_query('上面是什么？', recent, excluded=('reply:excluded:1',))
    assert '不能泄漏' not in plan.query
    assert plan.mode == 'ambiguous'


class IndexedMemory:
    """Real local original index, deliberately no external semantic provider."""
    enabled = False
    def __init__(self, root, *, user='local-user'):
        from runtime.memory.source_retrieval import SourceRetrieval
        from runtime.memory.conversation_memory_port import NullConversationMemoryPort
        self._originals = SourceRetrieval(root / 'original-text-index.sqlite3')
        self.config = SimpleNamespace(data_root=root, user_id=user, context_max_chars=2400)
        self._null = NullConversationMemoryPort()
    def status(self):
        return self._null.status()
    def browse_originals(self, *, user_id, query, limit):
        return self._originals.browse(user_id, query, limit)
    def index_original_exchange(self, **kwargs):
        self._originals.put(kwargs['user_id'], kwargs['source_id'], kwargs['user_message'],
                            kwargs['assistant_message'], kwargs['occurred_at'])
        return True
    def register_archive_sources(self, *, user_id, sources):
        self._originals.register_archive(user_id, sources)


def seed_archive(tmp_path, *, stamp=1710000000):
    from runtime.memory.local_memory import LocalMemoryAdapter
    from runtime.memory.memory_port import LegacyLetter
    archive = LocalMemoryAdapter(tmp_path / 'archive.sqlite3')
    result = archive.import_legacy_records([LegacyLetter(
        content='用户来信：外婆的围巾。\n林离回信：铜纽扣。',
        source_record_id='official:account:scarf', source='official-olivia', occurred_at=stamp,
        metadata={'import_kind': 'official_text_reply', 'official_history_publish_status': 'completed_v1',
                  'user_content': '外婆的围巾上缝着一颗铜纽扣。', 'reply_text': '那颗铜纽扣我记住了。'})])
    assert result.inserted == 1
    return archive


def production_adapter(archive, memory, recent=()):
    from local_server import LetterAdapter
    from llm_gateway import GatewayConfig
    from runtime.memory.memory_prompt import MemoryPromptBuilder
    adapter = LetterAdapter(GatewayConfig(provider='mock', feature_enabled=True,
        persona_v2_enabled=True, max_input_chars=100000), memory_port=archive, conversation_memory=memory)
    adapter.memory_prompt_builder = MemoryPromptBuilder(archive, conversation_memory=memory,
        max_results=100, max_tokens=300000, conversation_budget=100000, memory_lifecycle=SimpleNamespace(is_paused=lambda: False))
    adapter.recent_letter_fragments = lambda _query: tuple(recent)
    return adapter


class DynamicCheck:
    config = SimpleNamespace(provider='openai_compatible')
    def __init__(self):
        self.calls = []
    async def complete_scoped(self, prompt, **kwargs):
        self.calls.append(deepcopy(prompt))
        packet = json.loads(prompt[1]['content'])
        source = next(s for s in packet['sources'] if s['scope'] == 'historical_exchange'
                      and '铜纽扣' in s['text'])
        return SimpleNamespace(text=json.dumps({'reply_intent': 'recall_question',
            'direct_questions': [], 'findings': [{'topic': '围巾', 'status': 'confirmed',
            'event_stage': 'reported', 'finding': '原文提过围巾上的铜纽扣。',
            'citations': [{'source': source['source'], 'quote': '那颗铜纽扣我记住了。'}]}]}, ensure_ascii=False))


async def final_request(adapter, query, *, mode=None):
    from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
    from runtime.reply.reply_reviewer import NullReviewer
    from runtime.reply.reply_context import ReplyMode
    from runtime.reply.reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
    adapter.gateway = DynamicCheck()
    class Capture:
        def __init__(self):
            self.gateway = SimpleNamespace(adapter=adapter)
            self.request = None
        async def run(self, request):
            self.request = request
            return ReplyResult(request.request_id, ReplyState.COMPLETED, text='synthetic reply')
    capture = Capture()
    pipeline = ReplyPipeline(capture, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                             discover_runtime_ports=False)
    mode = mode or ReplyMode.TEXT_LETTER
    context = adapter.build_reply_context(mode, future_im_enabled=mode is ReplyMode.FUTURE_IM)
    result = await pipeline.run(ReplyRequest(content=query, max_input_chars=100000), context)
    assert result.state is ReplyState.COMPLETED, result
    return capture.request, adapter.gateway


@pytest.mark.parametrize('query,recent', [
    ('外婆的围巾上是什么？', ()),
    ('上面缝着什么？', (query_fragment('聊聊外婆留下的围巾。'),)),
    ('好久不见。', ()),
])
def test_restart_and_production_pipeline_preserve_archive_in_final_model_input(tmp_path, query, recent):
    from runtime.memory.local_memory import LocalMemoryAdapter
    from runtime.memory.memory_prompt import _unescape_reserved
    archive = seed_archive(tmp_path)
    archive.close()
    reopened = LocalMemoryAdapter(tmp_path / 'archive.sqlite3')
    try:
        adapter = production_adapter(reopened, IndexedMemory(tmp_path / 'index'), recent)
        request, check = asyncio.run(final_request(adapter, query))
        system = request.messages[0]['content']
        assert '外婆的围巾上缝着一颗铜纽扣。' in system
        assert '那颗铜纽扣我记住了。' in system
        assert '[ORIGINAL' in _unescape_reserved(system)
        assert request.messages[-1]['content'] == query
        assert len(check.calls) == 1
    finally:
        reopened.close()


def test_contextual_query_does_not_bypass_user_isolation(tmp_path):
    from runtime.memory.local_memory import LocalMemoryAdapter
    from runtime.memory.history_continuity import plan_history_query, add_history_tail
    from runtime.memory.recall import RecallResult
    from runtime.memory.memory_prompt import MemoryPromptBuilder
    source = seed_archive(tmp_path)
    other = LocalMemoryAdapter(tmp_path / 'other.sqlite3')
    try:
        builder = MemoryPromptBuilder(other, conversation_memory=IndexedMemory(tmp_path / 'other-index'))
        plan = plan_history_query('好久不见。')
        result = add_history_tail(builder, RecallResult(), plan, now=datetime.now(timezone.utc))
        assert result.records == ()
    finally:
        source.close(); other.close()


@pytest.mark.parametrize('alias', ['original', 'history', 'relationship'])
def test_tail_honors_forgotten_source_aliases(tmp_path, alias):
    from runtime.memory.history_continuity import plan_history_query, add_history_tail
    from runtime.memory.recall import RecallResult
    from runtime.memory.recall_sources import archive_source_aliases
    archive = seed_archive(tmp_path)
    try:
        memory = IndexedMemory(tmp_path / 'index')
        row = archive.list_legacy()[0]
        aliases = archive_source_aliases(row['source_record_id'], row['metadata'])
        source = (row['source_record_id'] if alias == 'original' else
                  next(s for s in aliases if s.startswith('history:') if 'relationship' not in s) if alias == 'history' else
                  next(s for s in aliases if s.startswith('relationship-letter:')))
        memory._originals.forget('local-user', source)
        builder = production_adapter(archive, memory).memory_prompt_builder
        result = add_history_tail(builder, RecallResult(), plan_history_query('好久不见。'), now=datetime.now(timezone.utc))
        assert result.records == ()
    finally:
        archive.close()


def test_undated_history_is_not_presented_as_the_latest_letter(tmp_path):
    from runtime.memory.history_continuity import plan_history_query, add_history_tail
    from runtime.memory.recall import RecallResult
    archive = seed_archive(tmp_path, stamp=None)
    try:
        builder = production_adapter(archive, IndexedMemory(tmp_path / 'index')).memory_prompt_builder
        result = add_history_tail(builder, RecallResult(), plan_history_query('上一封信写了什么？'), now=datetime.now(timezone.utc))
        assert result.records == ()
    finally:
        archive.close()


def test_zero_memory_budget_cannot_enable_the_history_tail(tmp_path):
    archive = seed_archive(tmp_path)
    try:
        memory = IndexedMemory(tmp_path / 'index')
        memory.config.context_max_chars = 0
        adapter = production_adapter(archive, memory)
        request, check = asyncio.run(final_request(adapter, '好久不见。'))
        assert '铜纽扣' not in repr(request.messages)
        assert check.calls == []
    finally:
        archive.close()


def test_diagnostics_record_actual_retained_groups_without_private_text():
    from runtime.diagnostics.recall_trace import snapshot, project
    original = messages()
    asyncio.run(prepare_recall_messages(original, LiveCheck(partial=True), max_input_chars=20000))
    record = snapshot()[-1]
    assert record['before_groups'] == record['final_groups'] == 2
    assert record['check_status'] == 'partial'
    assert record['unverified_topics'] == 1
    assert '铜纽扣' not in json.dumps(record, ensure_ascii=False)
    assert 'history:scarf' not in repr(record)
    assert 'secret' not in repr(project({'event': 'history_recall', 'query': 'secret', 'final_ids': ['secret'], 'source_status': {'secret': 'available'}}))


def test_official_http_import_index_backfill_restart_and_final_request(tmp_path, monkeypatch):
    import local_server as server
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from runtime.imports.official_letters import build_legacy_import_payload
    from runtime.memory.local_memory import LocalMemoryAdapter
    from runtime.memory.conversation_memory_port import NullConversationMemoryPort
    from runtime.memory.conversation_memory_delivery import ConversationMemoryDeliveryCommitter
    from runtime.memory.conversation_memory_outbox import CanonicalMemoryOutbox
    archive = LocalMemoryAdapter(tmp_path / 'archive.sqlite3')
    payload = build_legacy_import_payload([
        {'letter_id': 'scarf', 'created_at': 1710000000, 'replied_at': 1710000100,
         'content': '外婆的围巾上缝着一颗铜纽扣。', 'reply_content': '那颗铜纽扣我记住了。'},
        {'letter_id': 'cup', 'created_at': 1710000200, 'replied_at': 1710000300,
         'content': '杯底画着蓝色纸鹤。', 'reply_content': '我记住了，是蓝色纸鹤。'},
        {'letter_id': 'trip', 'created_at': 1710000400, 'replied_at': 1710000500,
         'content': '周五去海边的计划取消了。', 'reply_content': '那就不去了。'},
    ], account_id='synthetic-account')
    monkeypatch.setattr(server, 'collect_default_official_text_replies', lambda: payload)
    # The network preflight is separate; this test exercises the real ordered
    # migration in optional-provider-disabled mode, not a fake import result.
    monkeypatch.setattr(server, '_official_history_preflight_error', lambda: None)
    monkeypatch.setattr(server, 'conversation_memory_adapter', NullConversationMemoryPort())
    monkeypatch.setattr(server, 'memory_adapter', archive)
    monkeypatch.setattr(server, '_legacy_import_adapter', lambda: archive)
    monkeypatch.setattr(server, 'store', SimpleNamespace(letters=[], legacy_letters=[], personal_chats=[]))
    monkeypatch.setattr(server, '_official_import_progress', dict(server._official_import_progress))
    async def run():
        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', server.handler)
        async with TestClient(TestServer(app, access_log=None)) as client:
            for count in (3, 0):
                response = await client.post('/toy/letter/legacy/official-import', json={},
                    headers={'X-Olivia-Companion-Action': 'confirmed'})
                result = await response.json()
                assert response.status == 200, result
                assert result['data']['inserted'] == count
        index = IndexedMemory(tmp_path / 'index')
        committer = ConversationMemoryDeliveryCommitter(index)
        outbox = CanonicalMemoryOutbox(tmp_path / 'state.json', tmp_path / 'outbox.sqlite3',
                                      committer, archive_memory=archive)
        await outbox._index_archive_originals()
        await outbox._index_archive_originals()
        reopened_index = IndexedMemory(tmp_path / 'index')
        counts = reopened_index.browse_originals(user_id='local-user', query='', limit=0)
        assert counts['archive_total'] == counts['archive_indexed'] == 3
        assert counts['indexed_letters'] == 3
        archive.close()
        reopened = LocalMemoryAdapter(tmp_path / 'archive.sqlite3')
        try:
            adapter = production_adapter(reopened, reopened_index)
            request, check = await final_request(adapter, '围巾有什么？杯底画着什么？海边去了吗？')
            final = request.messages[0]['content']
            for text in ('外婆的围巾上缝着一颗铜纽扣。', '杯底画着蓝色纸鹤。', '周五去海边的计划取消了。', '那就不去了。'):
                assert text in final
            assert len(check.calls) == 1
        finally:
            reopened.close()
    try:
        asyncio.run(run())
    finally:
        archive.close()


def test_history_tail_read_failure_is_not_an_empty_archive(tmp_path, monkeypatch):
    from runtime.memory.history_continuity import plan_history_query, add_history_tail
    from runtime.memory.recall import RecallResult
    archive = seed_archive(tmp_path)
    try:
        memory = IndexedMemory(tmp_path / 'index')
        def fail(*_):
            import sqlite3
            raise sqlite3.OperationalError('private-path must not escape')
        monkeypatch.setattr(memory._originals, 'forgotten_sources', fail)
        builder = production_adapter(archive, memory).memory_prompt_builder
        result = add_history_tail(builder, RecallResult(), plan_history_query('好久不见。'), now=datetime.now(timezone.utc))
        assert result.status == 'unavailable'
        assert result.records == ()
        assert 'private-path' not in repr(result)
    finally:
        archive.close()


def test_undated_reunion_sample_stays_explicitly_undated(tmp_path):
    from runtime.memory.history_continuity import plan_history_query, add_history_tail
    from runtime.memory.recall import RecallResult
    archive = seed_archive(tmp_path, stamp=None)
    try:
        builder = production_adapter(archive, IndexedMemory(tmp_path / 'index')).memory_prompt_builder
        result = add_history_tail(builder, RecallResult(), plan_history_query('好久不见。'), now=datetime.now(timezone.utc))
        assert len(result.records) == 2
        assert all(r.occurred_at is None for r in result.records)
    finally:
        archive.close()


def test_diagnostic_bundle_keeps_only_safe_recall_fields():
    import io
    import zipfile
    from runtime.diagnostics.support_bundle import build_diagnostic_bundle
    source = {'summary': {'status': 'available'}, 'health': {'checks': {}, 'status': 'available'},
              'install': {'status': 'unknown'}, 'tasks': {'status': 'available', 'pending': 0, 'items': []}, 'launcher_tail': [], 'runtime_tail': [],
              'recall_tail': [{'event': 'history_recall', 'trace_id': 'a' * 32,
                               'before_groups': 2, 'final_groups': 2, 'query_mode': 'contextual',
                               'query': 'private-scarf', 'prompt': 'private-key'}]}
    bundle = build_diagnostic_bundle(source)
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        raw = archive.read('runtime-tail.jsonl')
        assert b'private-' not in raw
        record = json.loads(raw)
        assert record['before_groups'] == record['final_groups'] == 2


def test_first_import_rebinds_existing_index_worker_without_replaying_delivery(tmp_path, monkeypatch):
    import runtime.memory.conversation_memory_runtime as runtime
    from runtime.memory.conversation_memory_port import ConversationMemoryStatus
    from runtime.memory.memory_port import NullMemoryPort
    monkeypatch.setattr(runtime, '_RUNTIME', None)
    monkeypatch.setattr(runtime, '_RUNTIME_KEY', None)
    memory = IndexedMemory(tmp_path / 'memory' / 'mem0')
    memory.config.outbox_data_root = tmp_path
    monkeypatch.setattr(memory, 'status', lambda: ConversationMemoryStatus('available', True, 'mem0', 'qdrant-local'))
    archive = seed_archive(tmp_path)
    missing = NullMemoryPort()
    try:
        runtime.ensure_conversation_memory_runtime(missing, memory, environ={}, start_background=False)
        worker = runtime._RUNTIME
        assert worker.outbox.archive_memory is missing
        runtime.ensure_conversation_memory_runtime(archive, memory, environ={}, start_background=False)
        assert runtime._RUNTIME is worker  # No second worker or reset journal.
        assert worker.outbox.archive_memory is archive
        asyncio.run(worker.outbox._index_archive_originals())
        counts = memory.browse_originals(user_id='local-user', query='', limit=0)
        assert counts['archive_indexed'] == counts['archive_total'] == 1
    finally:
        runtime.stop_conversation_memory_runtime()
        archive.close()
