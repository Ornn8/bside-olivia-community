from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter
from runtime.memory.local_memory import LocalMemoryAdapter
from runtime.memory.companion_memory_context import CompanionMemoryPromptBuilder
from tests.memory.test_mem0_memory import FakeMem0,_config


def test_ring_question_keeps_both_speakers_inside_existing_prompt_budget(tmp_path):
    memory=Mem0ConversationMemoryAdapter(FakeMem0(),_config(tmp_path))
    memory._originals.put('local-user','history:ring',
        '你还记得我们的戒指吗？'+'那天我们沿着河边散步，看了很久的天空。'*18,
        '我记得那枚银色戒指，是那天散步时你送给我的。'+'后来我们又去看了花。'*35,None)
    for i in range(20):
        memory._originals.put('local-user',f'history:noise{i}',
            '你还记得我们那天一起走过吗？'+'还有很多别的事情。'*30,
            '我记得那次散步。'+'别的事情以后再说。'*30,None)
    archive=LocalMemoryAdapter(tmp_path/'archive.sqlite3')
    try:
        prompt=CompanionMemoryPromptBuilder(archive,memory).build('你还记得我们的戒指吗？')
        assert len(prompt.text)<=2400
        assert any(r.provenance.get('speaker')=='user' and '戒指' in r.text for r in prompt.references)
        assert any(r.provenance.get('speaker')=='linli' and '银色戒指' in r.text for r in prompt.references)
    finally:archive.close()
