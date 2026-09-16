from datetime import datetime, timezone
import sqlite3

from runtime.memory.source_retrieval import SourceRetrieval
from runtime.memory.conversation_memory_port import ConversationMemoryRecord

NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


def test_short_chinese_query_retrieves_original_and_isolates_user(tmp_path):
    index = SourceRetrieval(tmp_path / 'sources.sqlite3')
    index.put('alice', 'letter:1', '外婆送给我一架旧钢琴。', '我记住这件事了。', NOW)
    index.put('bob', 'letter:1', '外婆送了秘密礼物。', '谢谢。', NOW)
    found = index.search('钢琴', 'alice')
    assert len(found) == 1
    assert found[0].text == '外婆送给我一架旧钢琴。'
    assert found[0].metadata['speaker'] == 'user'
    assert found[0].occurred_at == NOW
    assert not index.search('秘密', 'alice')


def test_semantic_hit_recovers_original_when_words_differ(tmp_path):
    index = SourceRetrieval(tmp_path / 'sources.sqlite3')
    index.put('alice', 'letter:1', '我的琴是外婆送的。', '', NOW)
    fact = ConversationMemoryRecord(memory_id='fact:1', text='用户的钢琴来自祖母', user_id='alice', source_id='letter:1')
    found = index.search('乐器来历', 'alice', (fact,))
    assert found[0].text == '我的琴是外婆送的。'
    assert found[0].metadata['verbatim'] is True


def test_long_original_is_preserved_but_retrieval_is_bounded(tmp_path):
    index = SourceRetrieval(tmp_path / 'sources.sqlite3')
    text = '旧钢琴的故事。' * 1000
    index.put('alice', 'letter:1', text, '', NOW)
    index.put('alice', 'letter:1', text, '', NOW)
    with sqlite3.connect(index.path) as db:
        assert db.execute("SELECT text FROM originals WHERE actor='user'").fetchone()[0] == text
    found = index.search('钢琴', 'alice', limit=20)
    assert len(found) <= 5
    assert len({r.text for r in found}) == len(found)
    assert all(len(r.text) <= 360 for r in found)


def test_forget_prevents_original_reappearing_on_backfill(tmp_path):
    index = SourceRetrieval(tmp_path / 'sources.sqlite3')
    index.put('alice', 'letter:1', '外婆的钢琴', '', NOW)
    index.put('bob', 'letter:1', '外婆的钢琴', '', NOW)
    index.forget('alice')
    index.put('alice', 'letter:1', '外婆的钢琴', '', NOW)
    assert not index.search('钢琴', 'alice')
    assert index.search('钢琴', 'bob')


def test_prompt_budget_is_local_and_preserves_speaker(tmp_path):
    from tests.memory.test_mem0_memory import FakeMem0, _config
    from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter
    from runtime.memory.companion_memory_context import CompanionMemoryPromptBuilder
    from runtime.memory.memory_port import NullMemoryPort
    from runtime.memory.memory_prompt import estimate_memory_tokens
    backend = FakeMem0()
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))
    adapter.index_original_exchange(user_id='local-user', source_id='reply:original:1',
        user_message='这架钢琴是外婆送给我的生日礼物。', assistant_message='我会记得它的来历。', occurred_at=NOW)
    builder = CompanionMemoryPromptBuilder(NullMemoryPort(), adapter, max_tokens=1500)
    prompt = builder.build('钢琴是谁送的？')
    assert '外婆' in prompt.text
    assert 'speaker' in prompt.text
    assert estimate_memory_tokens(prompt.text) <= 1500
    assert not any(method == 'add' for method, _ in backend.calls)
    calls = len([1 for method, _ in backend.calls if method == 'search'])
    builder.build('钢琴是谁送的？')
    assert len([1 for method, _ in backend.calls if method == 'search']) == calls


def test_exclusion_happens_before_small_result_limit(tmp_path):
    index = SourceRetrieval(tmp_path / 'sources.sqlite3')
    for number in range(5):
        index.put('alice', f'letter:{number}', f'钢琴的第{number}个故事。', '', NOW)
    found = index.search('钢琴', 'alice', limit=1, exclude_source_ids=('letter:0', 'letter:1', 'letter:2', 'letter:3'))
    assert [record.source_id for record in found] == ['letter:4']


def test_original_backfill_obeys_lifecycle_without_calling_extraction():
    import asyncio
    from types import SimpleNamespace
    from runtime.memory.conversation_memory_delivery import ConversationMemoryDeliveryCommitter
    writes = []
    memory = SimpleNamespace(index_original_exchange=lambda **kwargs: writes.append(kwargs) or True)
    class Lifecycle:
        paused = True
        def run_write(self, operation, **kwargs):
            return None if self.paused else operation()
    lifecycle = Lifecycle()
    committer = ConversationMemoryDeliveryCommitter(memory, memory_lifecycle=lifecycle)
    delivery = SimpleNamespace(user_id='alice', source_id='reply:1', user_message='原文', assistant_message='回信', occurred_at=NOW)
    asyncio.run(committer.index_original(delivery))
    assert not writes
    lifecycle.paused = False
    asyncio.run(committer.index_original(delivery))
    asyncio.run(committer.index_original(delivery))
    assert len(writes) == 1


def test_terminal_old_letter_gets_original_index_without_paid_reextraction(tmp_path):
    import asyncio
    from tests.memory.test_conversation_memory_outbox import _state, _outbox, SequencedCommitter
    from tests.memory.test_mem0_memory import FakeMem0, _config
    from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter
    from runtime.memory.conversation_memory_delivery import ConversationMemoryDeliveryCommitter
    _state(tmp_path / 'state.json', user_text='外婆送我的钢琴。')
    asyncio.run(_outbox(tmp_path, SequencedCommitter()).scan_once())
    backend = FakeMem0()
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))
    result = asyncio.run(_outbox(tmp_path, ConversationMemoryDeliveryCommitter(adapter)).scan_once())
    assert result.duplicates == 1
    assert adapter.search_evidence_context('钢琴', user_id='local-user', limit=5)
    assert not any(method == 'add' for method, _ in backend.calls)


def test_long_original_still_supplies_detail_with_default_prompt_budget(tmp_path):
    from tests.memory.test_mem0_memory import FakeMem0, _config
    from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter
    from runtime.memory.companion_memory_context import CompanionMemoryPromptBuilder
    from runtime.memory.memory_port import NullMemoryPort
    from runtime.memory.memory_prompt import estimate_memory_tokens
    backend = FakeMem0()
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))
    adapter.index_original_exchange(user_id='local-user', source_id='history:offline:long',
        user_message='旧钢琴是外婆送我的。' + '那时候我每天都会坐在窗边练习曲子，也会把一天里的小事写下来。' * 20,
        assistant_message='谢谢你告诉我。', occurred_at=NOW)
    found = adapter.search_evidence_context('旧钢琴', user_id='local-user', limit=5)
    assert found
    prompt = CompanionMemoryPromptBuilder(NullMemoryPort(), adapter, max_tokens=1500).build('旧钢琴', max_chars=2400)
    assert '外婆' in prompt.text
    assert estimate_memory_tokens(prompt.text) <= 1500
