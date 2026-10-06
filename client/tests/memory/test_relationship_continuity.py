"""Delivered character statements survive retrieval windows and writer changes."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from runtime.memory.mem0_memory import Mem0Config, Mem0ConversationMemoryAdapter
from runtime.memory.conversation_memory_delivery import ConversationMemoryDeliveryCommitter
from runtime.memory.conversation_memory_outbox import CanonicalMemoryOutbox
from runtime.reply import jev_questions


NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)
USER = '我想认真跟你交往。'
PROMISE = '那就先当我的考察期男友吧。冬天冷的时候我愿意给你捂手。'
WITHDRAWAL = '考察期的说法我先收回，我们先做朋友。捂手也先不答应了。'


class Decisions:
    def __init__(self, decisions=None):
        self.calls = []
        self.decisions = decisions or {'identity': 'keep', 'affection': 'skip', 'agreement': 'keep'}

    def ask_sync(self, state, questions, *, purpose):
        self.calls.append((state, questions, purpose))
        return {key: self.decisions[key.split('_', 1)[1]] for key in questions}


def memory(tmp_path):
    return Mem0ConversationMemoryAdapter(SimpleNamespace(), Mem0Config(enabled=True,
        data_root=tmp_path, llm_base_url='http://127.0.0.1:9/v1', llm_model='fixture',
        embedding_cache=tmp_path / 'models'))


def index(adapter, source='reply:relationship:1', user=USER, reply=PROMISE, stamp=NOW):
    adapter.index_original_exchange(user_id='local-user', source_id=source,
        user_message=user, assistant_message=reply, occurred_at=stamp)


def packet(adapter, *, now=NOW, excluded=(), max_chars=4000):
    from runtime.memory.companion_memory_context import CompanionMemoryPromptBuilder
    from runtime.memory.memory_port import NullMemoryPort
    return json.loads(CompanionMemoryPromptBuilder(NullMemoryPort(), adapter).relationship_context(
        as_of=now, exclude_source_ids=excluded, max_chars=max_chars))


def test_old_completed_delivery_backfills_once_without_rewriting_user_facts(tmp_path, monkeypatch):
    adapter, decisions = memory(tmp_path / 'memory'), Decisions()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: decisions)
    state, journal = tmp_path / 'letters.json', tmp_path / 'outbox.sqlite3'
    state.write_text(json.dumps({'letters': [dict(letter_id='relationship', reply_revision=1,
        letter_status='COMPLETED', content=USER, reply_text=PROMISE,
        private_world_occurred_at=NOW.isoformat())]}), encoding='utf-8')
    committer = ConversationMemoryDeliveryCommitter(adapter)
    outbox = CanonicalMemoryOutbox(state, journal, committer)
    # This delivery was already marked written by the pre-fix user-only extractor.
    from runtime.memory.conversation_memory_delivery import CanonicalMemoryDeliveryResult, CanonicalMemoryDeliveryStatus
    from runtime.memory.conversation_memory_outbox import _delivery_from_row
    row = json.loads(state.read_text(encoding='utf-8'))['letters'][0]
    delivery = _delivery_from_row(row, user_id='local-user')
    outbox._record(delivery, CanonicalMemoryDeliveryResult(CanonicalMemoryDeliveryStatus.WRITTEN, delivery.source_id))
    asyncio.run(outbox.scan_once())
    asyncio.run(committer._relationship_call.settle_async(timeout_seconds=2))
    assert PROMISE in json.dumps(packet(adapter), ensure_ascii=False)
    asyncio.run(outbox.scan_once())
    assert len(decisions.calls) == 1
    assert decisions.calls[0][0]['exchanges'][0]['assistant_message'] == PROMISE
    assert decisions.calls[0][0]['exchanges'][0]['user_message'] == USER


def test_new_delivery_keeps_user_facts_and_character_relationship_separate(tmp_path, monkeypatch):
    from tests.memory.test_mem0_memory import FakeMem0, _config
    backend, decisions = FakeMem0(), Decisions()
    class BothRoutes(Decisions):
        def ask_sync(self, state, questions, *, purpose):
            if 'exchanges' in state:
                return decisions.ask_sync(state, questions, purpose=purpose)
            return dict.fromkeys(questions, 'keep')
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: BothRoutes())
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'personal_chats': [dict(letter_id='qq', reply_revision=1,
        letter_status='COMPLETED', delivery_status='DELIVERED', channel='qq',
        content=USER, reply_text=PROMISE, private_world_occurred_at=NOW.isoformat())]}), encoding='utf-8')
    committer = ConversationMemoryDeliveryCommitter(adapter)
    outbox = CanonicalMemoryOutbox(state, tmp_path / 'outbox.sqlite3', committer)
    result = asyncio.run(outbox.scan_once())
    asyncio.run(committer._relationship_call.settle_async(timeout_seconds=2))
    assert result.delivered == 1
    adds = [call for method, call in backend.calls if method == 'add']
    assert len(adds) == 1
    assert USER in adds[0]['messages'] and PROMISE not in adds[0]['messages']
    assert adds[0]['metadata']['actor'] == 'local_user'
    assert [r['speaker'] for r in packet(adapter)['records']] == ['user', 'linli']
    assert packet(adapter)['records'][1]['text'] == PROMISE
    asyncio.run(outbox.scan_once())
    assert len(decisions.calls) == 1


@pytest.mark.parametrize('mode', ['text_letter', 'voice_reply', 'spoken_video', 'singing_video',
                                  'voice_song_video', 'musical_video', 'future_im'])
def test_shared_prompt_keeps_original_identity_across_models_after_tail_expires(tmp_path, monkeypatch, mode):
    import local_server
    from llm_gateway import GatewayConfig
    from runtime.memory.memory_port import NullMemoryPort
    from runtime.memory.history_selection import _split
    decisions, adapter = Decisions(), memory(tmp_path)
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: decisions)
    index(adapter)
    adapter.refresh_relationship_memory(user_id='local-user')
    replies = [dict(letter_id='relationship', reply_revision=1, letter_status='COMPLETED',
        private_world_occurred_at=NOW.isoformat(), content=USER, reply_text=PROMISE)]
    replies += [dict(letter_id=f'chat{i}', reply_revision=1, letter_status='COMPLETED',
        private_world_occurred_at=(NOW + timedelta(minutes=i+1)).isoformat(),
        content='普通近况。' * 35, reply_text='我知道了。' * 35) for i in range(35)]
    outputs = []
    for model in ('fixture-flash', 'fixture-opus'):
        writer = local_server.LetterAdapter(GatewayConfig(provider='mock', model=model),
            memory_port=NullMemoryPort(), conversation_memory=adapter, recent_letters=lambda: replies)
        writer._now = lambda: NOW + timedelta(hours=2)
        # Read through the production context assembly, without an LLM call.
        fragments = writer.recent_letter_fragments('早上好')
        stable = next(f for f in fragments if f.fragment_id == 'chat.relationship')
        outputs.append(stable.text)
        assert PROMISE not in str([f.text for f in fragments if f.fragment_id != 'chat.relationship'])
        from runtime.reply.reply_context import ReplyMode
        messages = writer.reply_context_messages('早上好', mode=ReplyMode(mode))
        base, candidates = _split(messages)
        assert PROMISE in str(base)
        assert PROMISE not in str(candidates)  # It cannot be discarded as an optional keyword hit.
        assert 'recorded_utterance' in str(base)
        from runtime.reply.reply_model_quality import _assembled_memory_evidence
        assert PROMISE in _assembled_memory_evidence(messages)  # Review sees the same frozen originals.
    assert outputs[0] == outputs[1]
    assert len(decisions.calls) == 1  # Generation only reads locally.


def test_relationship_context_does_not_leak_other_users_or_classify_long_stories(tmp_path, monkeypatch):
    adapter, decisions = memory(tmp_path), Decisions()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: decisions)
    adapter.index_original_exchange(user_id='another-user', source_id='reply:private:1',
        user_message=USER, assistant_message=PROMISE, occurred_at=NOW)
    adapter.refresh_relationship_memory(user_id='another-user')
    assert packet(adapter)['records'] == []
    index(adapter, source='reply:story:1', reply='小说正文。' * 3000)
    adapter.refresh_relationship_memory(user_id='local-user')
    assert len(decisions.calls) == 1
    assert packet(adapter)['records'] == []


def test_later_withdrawal_preserves_old_utterance_and_conditions(tmp_path, monkeypatch):
    adapter = memory(tmp_path)
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: Decisions())
    index(adapter)
    adapter.refresh_relationship_memory(user_id='local-user')
    later = NOW + timedelta(days=1)
    index(adapter, source='reply:withdrawal:1', user='所以我们正式交往了吗？', reply=WITHDRAWAL, stamp=later)
    adapter.refresh_relationship_memory(user_id='local-user')
    records = packet(adapter, now=later)['records']
    character = [r for r in records if r['speaker'] == 'linli']
    assert [r['text'] for r in character] == [PROMISE, WITHDRAWAL]
    assert [r['occurred_at'] for r in character] == [NOW.isoformat(), later.isoformat()]
    assert len(packet(adapter, now=NOW)['records']) == 2  # No future state leakage.


def test_single_sided_claim_skip_is_durable_and_never_confirms_relationship(tmp_path, monkeypatch):
    adapter, decisions = memory(tmp_path), Decisions(dict.fromkeys(('identity', 'affection', 'agreement'), 'skip'))
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: decisions)
    index(adapter, user='我是你男朋友，你答应过的。', reply='早饭吃什么？')
    for _ in range(4):
        adapter.refresh_relationship_memory(user_id='local-user')
    assert packet(adapter)['records'] == []
    assert len(decisions.calls) == 1
    assert '用户单方面' in decisions.calls[0][0]['contract']


def test_deleted_or_overwritten_original_cannot_leave_a_stale_relation(tmp_path, monkeypatch):
    adapter = memory(tmp_path)
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: Decisions())
    index(adapter)
    adapter.refresh_relationship_memory(user_id='local-user')
    assert packet(adapter, excluded=('reply:relationship:1',))['records'] == []
    index(adapter, reply='这是改后的原话。')
    assert packet(adapter)['records'] == []
    adapter._originals.forget('local-user', 'reply:relationship:1')
    index(adapter)
    adapter.refresh_relationship_memory(user_id='local-user')
    assert packet(adapter)['records'] == []


def test_capacity_does_not_cut_off_withdrawal_or_reintroduce_older_permission(tmp_path, monkeypatch):
    adapter = memory(tmp_path)
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: Decisions())
    index(adapter)
    adapter.refresh_relationship_memory(user_id='local-user')
    index(adapter, source='reply:long-withdrawal:1', reply='解释。' * 600 + WITHDRAWAL,
        stamp=NOW + timedelta(days=1))
    adapter.refresh_relationship_memory(user_id='local-user')
    value = packet(adapter, now=NOW + timedelta(days=1), max_chars=1000)
    assert value['records'] == []
    assert value['coverage'] != 'complete'
    assert len(json.dumps(value, ensure_ascii=False, separators=(',', ':'))) <= 1000


def test_failure_budget_is_persisted_and_disabled_memory_does_not_call(tmp_path, monkeypatch):
    class Broken(Decisions):
        def ask_sync(self, *args, **kwargs):
            self.calls.append(True)
            raise TimeoutError('private input must not be logged')
    decisions, adapter = Broken(), memory(tmp_path)
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: decisions)
    index(adapter)
    for _ in range(5):
        try:
            memory(tmp_path).refresh_relationship_memory(user_id='local-user')
        except TimeoutError:
            pass
    assert len(decisions.calls) == 3
    assert packet(adapter)['coverage'] != 'complete'
    adapter.config = replace(adapter.config, context_max_chars=0)
    assert packet(adapter)['records'] == []
    adapter.refresh_relationship_memory(user_id='local-user')
    assert len(decisions.calls) == 3


def test_slow_extraction_does_not_block_generation_pause_or_duplicate_calls(tmp_path, monkeypatch):
    import threading
    from runtime.memory.conversation_memory_admin import ConversationMemoryAdminService
    from runtime.memory.companion_memory_context import CompanionMemoryPromptBuilder
    from runtime.memory.memory_port import NullMemoryPort
    entered, release = threading.Event(), threading.Event()
    class Slow(Decisions):
        def ask_sync(self, state, questions, *, purpose):
            result = super().ask_sync(state, questions, purpose=purpose)
            entered.set()
            assert release.wait(5)
            return result
    adapter, decisions = memory(tmp_path), Slow()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: decisions)
    index(adapter)
    lifecycle = ConversationMemoryAdminService(adapter, tmp_path / 'admin.sqlite3')
    committer = ConversationMemoryDeliveryCommitter(adapter, memory_lifecycle=lifecycle)
    async def exercise():
        try:
            await committer.refresh_relationship_memory('local-user')
            assert await asyncio.to_thread(entered.wait, 1)
            assert await asyncio.wait_for(asyncio.to_thread(lifecycle.is_paused), 1) is False
            for _ in range(5):
                await committer.refresh_relationship_memory('local-user')
            assert len(decisions.calls) == 1
            await asyncio.wait_for(asyncio.to_thread(lifecycle.pause,
                request_id='pause.fixture', reason='User paused memory during extraction'), 1)
        finally:
            release.set()
        await committer._relationship_call.settle_async(timeout_seconds=2)
    asyncio.run(exercise())
    assert packet(adapter)['records'] == []  # The late result could not commit after pause.
    builder = CompanionMemoryPromptBuilder(NullMemoryPort(), adapter, memory_lifecycle=lifecycle)
    assert json.loads(builder.relationship_context(as_of=NOW))['records'] == []


def test_deleted_alias_during_extraction_cannot_be_resurrected(tmp_path, monkeypatch):
    adapter = memory(tmp_path)
    adapter.index_received_user(user_id='local-user', source_id='received-user:receipt',
        user_message=USER, occurred_at=NOW, exchange_sources=('reply:relationship:1',))
    index(adapter)
    class DeleteDuringCall(Decisions):
        def ask_sync(self, *args, **kwargs):
            adapter._originals.forget('local-user', 'received-user:receipt')
            return super().ask_sync(*args, **kwargs)
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: DeleteDuringCall())
    adapter.refresh_relationship_memory(user_id='local-user')
    index(adapter)
    assert packet(adapter)['records'] == []


def test_backfill_batches_originals_and_persists_skips_across_restart(tmp_path, monkeypatch):
    import threading
    class BatchDecisions:
        calls = []
        def ask_sync(self, state, questions, *, purpose):
            self.calls.append((state, questions))
            assert purpose == 'memory-extraction'
            assert len(state['exchanges']) <= 8
            assert len(questions) == 3 * len(state['exchanges'])
            answers = {}
            for i, original in enumerate(state['exchanges']):
                keep = original['assistant_message'] in {PROMISE, WITHDRAWAL}
                for category in ('identity', 'affection', 'agreement'):
                    answers[f'r{i}_{category}'] = 'keep' if keep and category != 'affection' else 'skip'
            return answers
    decisions, adapter = BatchDecisions(), memory(tmp_path)
    # This exercises synchronous refresh; late workers from other fixtures must
    # not share its counting fake or contribute unrelated calls.
    owner = threading.current_thread()
    monkeypatch.setattr(jev_questions, 'configured_questions',
                        lambda: decisions if threading.current_thread() is owner else None)
    for i in range(24):
        reply = PROMISE if i == 0 else WITHDRAWAL if i == 12 else '今天聊普通近况。'
        index(adapter, source=f'reply:backfill{i}:1', reply=reply, stamp=NOW + timedelta(minutes=i))
    for _ in range(6):
        memory(tmp_path).refresh_relationship_memory(user_id='local-user')
    assert len(decisions.calls) == 3
    assert [o['source_id'] for o in decisions.calls[0][0]['exchanges']] == [f'reply:backfill{i}:1' for i in reversed(range(16, 24))]
    value = packet(adapter, now=NOW + timedelta(hours=1))
    assert [r['text'] for r in value['records'] if r['speaker'] == 'linli'] == [PROMISE, WITHDRAWAL]
    assert value['coverage'] == 'bounded'


def test_backfill_request_keeps_whole_originals_within_wire_capacity(tmp_path, monkeypatch):
    from runtime.reply.jev_questions import JevQuestionsPort, SEMANTIC_REQUEST_MAX_BYTES
    class SizedDecisions:
        calls = []
        def ask_sync(self, state, questions, *, purpose):
            originals = state['exchanges']
            assert sum(len(o['user_message']) + len(o['assistant_message']) for o in originals) <= 12000
            assert JevQuestionsPort('http://127.0.0.1:9/v1/companion/decide').request_size_bytes(
                state, questions, purpose=purpose) <= SEMANTIC_REQUEST_MAX_BYTES
            self.calls.append(originals)
            return dict.fromkeys(questions, 'skip')
    decisions, adapter = SizedDecisions(), memory(tmp_path)
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: decisions)
    for i in range(10):
        index(adapter, source=f'reply:capacity{i}:1', user='用户。' * 600,
            reply='回复。' * 600 + str(i), stamp=NOW + timedelta(minutes=i))
    for _ in range(8):
        adapter.refresh_relationship_memory(user_id='local-user')
    offered = [o for batch in decisions.calls for o in batch]
    assert len(offered) == 10
    assert len({o['source_id'] for o in offered}) == 10
    assert all(o['assistant_message'] == '回复。' * 600 + o['source_id'].split('capacity')[1].split(':')[0] for o in offered)


def test_large_completed_journal_is_read_once_and_backfills_newest_first(tmp_path, monkeypatch):
    from contextlib import closing
    from runtime.memory.conversation_memory_outbox import _delivery_from_row
    # This is a steady-state scan: canonical extraction already succeeded.
    # No wall-clock assertion; connection count catches N SQLite opens reliably.
    class IndexedOnly:
        indexed = []
        async def index_original(self, delivery):
            self.indexed.append(delivery.source_id)
            return True
        async def commit(self, delivery):
            raise AssertionError('a terminal exchange cannot be reclassified')
    rows = [dict(letter_id=f'scale{i}', reply_revision=1, letter_status='COMPLETED',
        content=USER, reply_text=PROMISE, private_world_occurred_at=(NOW + timedelta(minutes=i)).isoformat())
        for i in range(1000)]
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'letters': rows}), encoding='utf-8')
    committer = IndexedOnly()
    outbox = CanonicalMemoryOutbox(state, tmp_path / 'outbox.sqlite3', committer)
    with closing(outbox._connect()) as db, db:
        db.executemany('INSERT INTO canonical_memory_deliveries VALUES (?,?,?,?,?,?,?)',
            [(d.source_id, d.letter_id, d.revision, 'written', 1, None, NOW.isoformat())
             for d in (_delivery_from_row(r, user_id='local-user') for r in rows)])
    connect, opened = outbox._connect, []
    def counted():
        opened.append(True)
        return connect()
    monkeypatch.setattr(outbox, '_connect', counted)
    result = asyncio.run(outbox.scan_once())
    assert result.duplicates == 1000
    assert len(opened) == 1
    assert committer.indexed == [f'reply:scale{i}:1' for i in reversed(range(980, 1000))]


def test_paused_backfill_does_not_consume_attempts_or_make_calls(tmp_path, monkeypatch):
    from contextlib import closing
    from runtime.memory.conversation_memory_admin import ConversationMemoryAdminService
    adapter, decisions = memory(tmp_path), Decisions()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: decisions)
    index(adapter)
    lifecycle = ConversationMemoryAdminService(adapter, tmp_path / 'admin.sqlite3')
    lifecycle.pause(request_id='pause.fixture', reason='User paused memory')
    assert not adapter.refresh_relationship_memory(user_id='local-user', memory_lifecycle=lifecycle)
    assert decisions.calls == []
    with closing(adapter._originals.connect()) as db:
        assert db.execute('SELECT attempts FROM relationship_quotes').fetchone() == (0,)


def test_background_batch_persists_all_labels_in_one_transaction(tmp_path, monkeypatch):
    adapter, decisions = memory(tmp_path), Decisions()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: decisions)
    for i in range(8):
        index(adapter, source=f'reply:batch{i}:1', stamp=NOW + timedelta(minutes=i))
    connect, opened = adapter._originals.connect, []
    def counted():
        opened.append(True)
        return connect()
    monkeypatch.setattr(adapter._originals, 'connect', counted)
    adapter.refresh_relationship_memory(user_id='local-user')
    assert len(opened) <= 2  # Claim plus one save, rather than one commit per row.
    from contextlib import closing
    with closing(connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM relationship_quotes WHERE categories IS NOT NULL').fetchone() == (8,)
