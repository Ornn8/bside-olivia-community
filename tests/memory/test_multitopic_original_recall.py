from datetime import datetime, timezone
from pathlib import Path
import pytest

from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter
from runtime.memory.companion_memory_context import CompanionMemoryPromptBuilder
from tests.memory.test_mem0_memory import FakeMem0, _config
from memory_port import NullMemoryPort


def fixture(tmp_path):
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))
    topics = ['培根早餐', '排骨香醋', '民政登记', '银白戒指', '名字离别']
    for i, topic in enumerate(topics):
        memory._originals.put('local-user', f'history:topic{i}',
            f'我们说过{topic}。' + '那天下午的风很轻。' * 30,
            f'{topic}我记得。' + '我也记得那时说的话。' * 25 + f'更正标记{i}：当时只是计划，并未完成。',
            datetime(2026, 9, i + 1, tzinfo=timezone.utc))
    return memory, topics


def test_all_topics_and_complete_answers_reach_final_persona(tmp_path):
    from local_server import LetterAdapter
    from llm_gateway import GatewayConfig
    memory, topics = fixture(tmp_path)
    query = '今天工作有很多事情想告诉你。' * 30 + '。'.join(topics)
    adapter = LetterAdapter(GatewayConfig(provider='mock', persona_v2_enabled=True,
        persona_v2_file=str(Path(__file__).resolve().parents[2] / 'linli_character/persona_release_v2.json')),
        memory_port=NullMemoryPort())
    adapter.memory_prompt_builder = CompanionMemoryPromptBuilder(NullMemoryPort(), memory)
    messages = adapter._persona_v2_messages(query)
    system = messages[0]['content']
    from runtime.reply.reply_model_quality import _assembled_memory_evidence
    review_evidence = _assembled_memory_evidence(messages)
    for i, topic in enumerate(topics):
        assert f'{topic}我记得' in system
        assert f'我们说过{topic}' in system
        assert f'更正标记{i}' in system
        assert f'更正标记{i}' in review_evidence
        assert f'2026-09-0{i+1}' in system
    assert sum(len(m['content']) for m in messages) <= adapter.config.max_input_chars


def test_expanded_recall_is_scoped_and_forgetting_wins(tmp_path):
    memory, topics = fixture(tmp_path)
    memory._originals.put('other-user', 'history:private', topics[0], 'PRIVATE', None)
    memory._originals.forget('local-user', 'history:topic0')
    records = memory.search_evidence_context('。'.join(topics), user_id='local-user', limit=100,
        exclude_source_ids=('history:topic1',))
    assert records
    assert all(r.source_id not in {'history:topic0', 'history:topic1', 'history:private'} for r in records)


def test_overflow_keeps_exchanges_together(tmp_path):
    memory, topics = fixture(tmp_path)
    prompt = CompanionMemoryPromptBuilder(NullMemoryPort(), memory).build('。'.join(topics), max_chars=4000)
    assert prompt.truncated
    assert len(prompt.text) <= 4000
    sources = [r.provenance['source_record_id'] for r in prompt.references]
    assert sources and all(sources.count(s) == 2 for s in sources)
    assert 'absence is not evidence' in prompt.text


def test_old_shipped_capacity_upgrades_but_explicit_environment_limit_wins(tmp_path):
    import json
    from llm_gateway import load_gateway_config
    path = tmp_path / 'llm.json'
    path.write_text(json.dumps({'max_input_chars': 30000}), encoding='utf-8')
    assert load_gateway_config(path, environ={}).max_input_chars == 100000
    assert load_gateway_config(path, environ={'OLIVIA_LLM_MAX_INPUT_CHARS': '30000'}).max_input_chars == 30000
    path.write_text(json.dumps({'max_input_chars': 16000}), encoding='utf-8')
    assert load_gateway_config(path, environ={}).max_input_chars == 16000


@pytest.mark.parametrize('mode', ['text_letter', 'voice_reply', 'spoken_video',
    'singing_video', 'voice_song_video', 'musical_video', 'future_im'])
def test_production_pipeline_shares_recall_and_world_across_modes(tmp_path, mode):
    from types import SimpleNamespace
    from local_server import LetterAdapter
    from llm_gateway import GatewayConfig
    from reply_context import ReplyMode
    from reply_orchestrator import ReplyRequest
    from runtime.reply.reply_pipeline import _prepare_generation_request
    from persona_assembly import UntrustedFragment
    memory, topics = fixture(tmp_path)
    adapter = LetterAdapter(GatewayConfig(provider='mock'), memory_port=NullMemoryPort())
    adapter.memory_prompt_builder = CompanionMemoryPromptBuilder(NullMemoryPort(), memory)
    adapter.daily_life_fragments = lambda content: (UntrustedFragment('world.fixture', 'WORLD_STATE_FIXTURE'),)
    context = adapter.build_reply_context(ReplyMode(mode), future_im_enabled=True)
    prepared = _prepare_generation_request(ReplyRequest(content='。'.join(topics)), context,
        SimpleNamespace(gateway=SimpleNamespace(adapter=adapter)))
    system = prepared.request.messages[0]['content']
    assert 'WORLD_STATE_FIXTURE' in system
    for i in range(len(topics)):
        assert f'更正标记{i}' in system


@pytest.mark.parametrize('channel,proactive', [('qq', False), ('qq', True), ('wechat', False), ('wechat', True)])
def test_social_presentation_retains_shared_relationship_and_originals(tmp_path, channel, proactive):
    from types import SimpleNamespace
    from local_server import LetterAdapter
    from llm_gateway import GatewayConfig
    from reply_context import ReplyMode
    from reply_orchestrator import ReplyRequest
    from runtime.reply.reply_pipeline import _prepare_generation_request
    from runtime.personal_chat.presentation import CURRENT
    from private_world_port import PrivateWorldSnapshot
    memory, topics = fixture(tmp_path)
    adapter = LetterAdapter(GatewayConfig(provider='mock'), memory_port=NullMemoryPort(),
        private_world_port=SimpleNamespace(snapshot=lambda: PrivateWorldSnapshot(relationship_stage='close', trust=80)))
    adapter.memory_prompt_builder = CompanionMemoryPromptBuilder(NullMemoryPort(), memory)
    token = CURRENT.set({'channel': channel, 'proactive': proactive, 'structured': True})
    try:
        context = adapter.build_reply_context(ReplyMode.FUTURE_IM, future_im_enabled=True)
        prepared = _prepare_generation_request(ReplyRequest(content='。'.join(topics)), context,
            SimpleNamespace(gateway=SimpleNamespace(adapter=adapter)))
        system = prepared.request.messages[0]['content']
        assert 'close' in system
        assert '更正标记4' in system
    finally:
        CURRENT.reset(token)


def test_song_lyrics_use_same_world_and_original_context(tmp_path):
    from local_server import LetterAdapter
    from llm_gateway import GatewayConfig
    from persona_assembly import UntrustedFragment
    from runtime.media.song_content import _planning_messages
    memory, topics = fixture(tmp_path)
    adapter = LetterAdapter(GatewayConfig(provider='mock'), memory_port=NullMemoryPort())
    adapter.memory_prompt_builder = CompanionMemoryPromptBuilder(NullMemoryPort(), memory)
    adapter.daily_life_fragments = lambda content: (UntrustedFragment('world.fixture', 'WORLD_STATE_FIXTURE'),)
    messages = _planning_messages('。'.join(topics), 110, adapter.config, reply_adapter=adapter)
    assert 'WORLD_STATE_FIXTURE' in messages[0]['content']
    for i in range(len(topics)):
        assert f'更正标记{i}' in messages[0]['content']
    assert sum(len(m['content']) for m in messages) <= adapter.config.max_input_chars
