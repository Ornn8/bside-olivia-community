"""Real send/persist/next-send boundary with only the external LLM replaced."""
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('query', [
    '我们在青岛一起听过那场建筑讲座吗？',
    '青岛那场建筑讲座是我们的共同经历，还是我编的？',
    '还记得我的口味吗？',
    'Do you remember my food preferences?',
])
def test_fact_recall_uses_originals_not_assistant_claims_about_the_user(query):
    from runtime.reply.recent_correspondence import recent_correspondence
    rows = [{"letter_id": "one", "reply_revision": 1, "letter_status": "COMPLETED",
             "private_world_occurred_at": "2026-09-05T01:00:00+00:00",
             "content": "青岛一起听讲座只是我编的假设。我喜欢酸口和微辣。",
             "reply_text": "你已经问过七遍了，你就是在试探我。"}]
    context = recent_correspondence(rows, query=query)
    assert rows[0]['content'] in [item['user_letter'] for item in json.loads(context)['letters']]
    assert rows[0]['reply_text'] not in context


@pytest.mark.parametrize('query', [
    '你刚才说的话是什么意思？',
    '你上次答应录给我的是什么？',
    'What did you say in your last reply?',
    '你问我的那个问题，我想好了。',
    '你之前说要录音，是哪封信说的？',
    '今天我有点累，想跟你说说。',
])
def test_dialogue_and_explicit_reply_reference_keep_canonical_reply(query):
    from runtime.reply.recent_correspondence import recent_correspondence
    rows = [{"letter_id": "one", "reply_revision": 1, "letter_status": "COMPLETED",
             "private_world_occurred_at": "2026-09-05T01:00:00+00:00",
             "content": "你愿意告诉我现在的想法吗？",
             "reply_text": "我想先把第二段练顺，再录给你。你愿意等吗？"}]
    context = recent_correspondence(rows, query=query)
    assert json.loads(context)['letters'][0]['linli_reply'] == rows[0]['reply_text']


def test_explicit_reference_can_retrieve_an_older_reply_without_treating_it_as_user_fact():
    from runtime.reply.recent_correspondence import recent_correspondence
    rows = [{"letter_id": str(index), "reply_revision": 1, "letter_status": "COMPLETED",
             "private_world_occurred_at": f"2026-09-05T01:0{index}:00+00:00",
             "content": '你好。',
             "reply_text": '我说过你是在试探，但这只是我的猜测。' if index == 0 else '晚安。'}
            for index in range(4)]
    context = json.loads(recent_correspondence(rows, query='你前面说我在试探，是什么意思？'))
    assert context['purpose'] == 'reply_reference'
    assert any(item.get('linli_reply') == rows[0]['reply_text'] for item in context['letters'])
    assert len(context['letters']) == 1  # Later chatter is not evidence for that earlier judgment.


def test_older_recall_does_not_reinforce_historical_assistant_inventions():
    from runtime.reply.recent_correspondence import recent_correspondence
    rows = [{"letter_id": str(index), "reply_revision": 1, "letter_status": "COMPLETED",
             "private_world_occurred_at": f"2026-09-05T01:0{index}:00+00:00",
             "content": "那次剧院演出只是我编造的场景。" if index == 0 else f"今天读第{index}本书。",
             "reply_text": "你已经问了七遍，答案始终一样。" if index == 0 else "慢慢读。"}
            for index in range(5)]
    context = recent_correspondence(rows, query="那次剧院演出发生过吗？")
    assert "只是我编造的场景" in context
    assert "七遍" not in context
    assert "慢慢读" not in context  # Factual recall must not use her earlier prose as proof.


def test_sparse_history_does_not_claim_adjacency_from_retrieval_order():
    from runtime.reply.recent_correspondence import recent_correspondence
    rows = [{"letter_id": str(index), "reply_revision": 1, "letter_status": "COMPLETED",
             "private_world_occurred_at": f"2026-09-05T01:0{index}:00+00:00",
             "content": '我独自去过青岛；一起听讲座只是想象。' if index == 0 else '今天看书。',
             "reply_text": '收到。'} for index in range(6)]
    text = recent_correspondence(rows, query='我们一起听过青岛那场讲座吗？')
    packet = json.loads(text)
    assert packet['letters'][0]['source_id'] == 'reply:0:1'
    assert packet['letters'][-1]['source_id'] == 'reply:5:1'
    assert len(packet['letters']) <= 4 and len(text) <= 2800
    assert 'time 仅为回信完成时间' in packet['meaning']
    assert '不另加日期或相邻序号' in packet['meaning']
    assert all('linli_reply' not in item for item in packet['letters'])


@pytest.mark.parametrize('query', [
    '一起听讲座只是我编的，这个限定是哪封信的内容？',
    '我不吃香菜，是哪一封信提到的？',
    'Which letter mentioned my food preferences?',
])
def test_source_lookup_discloses_originals_not_reply_times_or_assistant_attribution(query):
    from runtime.reply.recent_correspondence import recent_correspondence
    rows = [{"letter_id": 'original', "reply_revision": 1, "letter_status": "COMPLETED",
             "private_world_occurred_at": '2026-09-05T12:50:00+00:00',
             "content": '一起听讲座是我编的。我不吃香菜。',
             "reply_text": '你在上一封信里说过，时间是12:50。'}]
    packet = json.loads(recent_correspondence(rows, query=query))
    assert packet['purpose'] == 'source_attribution'
    assert packet['letters'] == [{'user_letter': rows[0]['content']}]
    assert '不标具体时刻、第几封、上一封或前一封' in packet['meaning']
    assert '不综述其他历史事实' in packet['meaning']
    assert '回信完成时间' not in packet['meaning']


@pytest.mark.parametrize('query', ['记得昨天我参加什么、今天做了什么吗？', '今天心情挺好。', '你刚才说的那件事呢？'])
def test_relative_dates_remain_bound_to_original_context_across_turns(query):
    from runtime.reply.recent_correspondence import recent_correspondence
    rows = [{"letter_id": str(index), "reply_revision": 1, "letter_status": "COMPLETED",
             "private_world_occurred_at": f"2026-09-05T13:2{index}:00+00:00",
             "content": text, "reply_text": '收到。'} for index, text in enumerate([
                 '昨天我参加了茶会。', '今天我旁听读书会。'])]
    text = recent_correspondence(rows, query=query)
    packet = json.loads(text)
    assert '后续回合不等于又过一天' in packet['meaning']
    assert '原信中的今天、昨天属于当时语境' in packet['meaning']
    assert '不确定时引用原文时间说法' in packet['meaning']
    assert len(text) <= 2800


@pytest.mark.parametrize('repeated_lookups', [0, 8])
def test_source_comparison_keeps_both_original_statement_and_related_question(repeated_lookups):
    from runtime.reply.recent_correspondence import recent_correspondence
    contents = [
        '一起听青岛建筑讲座只是我编的假设，不是真的，我只看到海报。',
        '补充真事：我独自去过青岛，上周在苏州当听众，南京没说过。',
        '今天吃面，喝温水。',
        '青岛我自己去过吗？我们一起听过讲座吗？苏州去了没有？南京去过没有？',
        '我去练琴了。', '晚安。',
    ]
    contents += ['一起听青岛讲座是我编的，这个限定出自哪封信？核对青岛苏州南京的提问里直接写了吗？'] * repeated_lookups
    rows = [{"letter_id": str(index), "reply_revision": 1, "letter_status": "COMPLETED",
             "private_world_occurred_at": f"2026-09-05T01:{index:02d}:00+00:00",
             "content": body, "reply_text": '以前说过。'} for index, body in enumerate(contents)]
    packet = json.loads(recent_correspondence(rows, query='一起听讲座是我编的，这个限定出自哪封信？核对青岛苏州南京的提问里直接写了吗？'))
    originals = [row['user_letter'] for row in packet['letters']]
    assert contents[0] in originals
    assert contents[3] in originals  # Assertion ranking alone drops this question.
    assert contents[5] not in originals
    assert len(originals) <= 4


def test_repeated_questions_do_not_displace_the_original_factual_correction():
    from runtime.reply.recent_correspondence import recent_correspondence
    query = "我们一起看过那场演出吗？"
    contents = ["我们一起看演出只是我编的场景，不是真的。", query, query, query,
                "今天换了一本小说。", "晚安，我去休息。"]
    rows = [{"letter_id": str(index), "reply_revision": 1, "letter_status": "COMPLETED",
             "private_world_occurred_at": f"2026-09-05T01:0{index}:00+00:00", "content": content,
             "reply_text": "收到。"} for index, content in enumerate(contents)]
    evidence = json.loads(recent_correspondence(rows, query=query))
    assert evidence['coverage'] == 'partial_canonical_correspondence'
    assert all(item['source_id'] == f"reply:{contents.index(item['user_letter'])}:1"
               for item in evidence['letters'])
    assert any("只是我编的场景" in item["user_letter"] for item in evidence["letters"])
    assert all(item["user_letter"] != query for item in evidence["letters"])


@pytest.mark.parametrize('body', [
    '你练琴了吗？我今天练了两次左手，但第二次没有练完。',
    'Did you practise? I practised the left hand twice, but did not finish the second time.',
])
def test_retrieval_keeps_explicit_counts_and_the_whole_mixed_letter(body):
    from runtime.reply.recent_correspondence import recent_correspondence
    rows = [{"letter_id":str(index), "reply_revision":1, "letter_status":"COMPLETED",
             "private_world_occurred_at":f"2026-09-05T01:0{index}:00+00:00",
             "content":body if index==0 else '晚安。', "reply_text":"收到。"} for index in range(4)]
    result = recent_correspondence(rows, query='左手练了几次？ How often did I practise the left hand?')
    assert body in [item['user_letter'] for item in json.loads(result)['letters']]
    assert len(result) <= 2800


def test_question_with_personal_fact_remains_retrievable_when_no_statement_matches():
    from runtime.reply.recent_correspondence import recent_correspondence
    body = '我对花生过敏，你能帮我记住吗？'
    rows = [{"letter_id": str(index), "reply_revision": 1, "letter_status": "COMPLETED",
             "private_world_occurred_at": f"2026-09-05T01:0{index}:00+00:00",
             "content": body if index == 0 else '今天看完了一本小说。',
             "reply_text": '收到。'} for index in range(4)]
    context = recent_correspondence(rows, query='你记得我的花生过敏吗？')
    assert body in [item['user_letter'] for item in json.loads(context)['letters']]


@pytest.mark.parametrize('gap', [0, 4])
def test_next_http_letter_keeps_recent_canonical_context_without_mem0(tmp_path, gap):
    script = r'''
import asyncio, json, os
from types import SimpleNamespace
from aiohttp.test_utils import TestClient, TestServer
import local_server as server
from original_client_server import create_configured_original_client_server_runtime
calls=[]
class ExternalModel:
    def timeout_seconds_for_scope(self, scope, *, default):
        return default
    async def complete(self, messages, **kwargs):
        return await self.complete_scoped(messages, **kwargs)
    async def complete_scoped(self, messages, **kwargs):
        calls.append(messages)
        return SimpleNamespace(text='你已经问过七遍了，答案一直一样。')
server.letters_adapter.gateway=ExternalModel()
async def main():
    runtime=create_configured_original_client_server_runtime(server_module=server)
    async with TestClient(TestServer(runtime.app)) as client:
        contents = ['我们去年在大阪看演出只是我编的场景，不是真的。']
        contents += [f'今晚我在收拾桌子，换了第{i}本书。' for i in range(int(os.environ['TEST_GAP']))]
        contents += ['我们去年在大阪看的那个演出是真的发生过吗？']
        for content in contents:
            response=await client.post('/toy/letter/send', json={'content':content})
            assert response.status==200, await response.text()
            letter=next(row for row in server.store.letters if row['content']==content)
            for _ in range(100):
                if letter['letter_status'] in {'COMPLETED','FAILED'}: break
                await asyncio.sleep(.01)
            assert letter['letter_status']=='COMPLETED', letter.get('error_code')
    print(json.dumps({'calls':calls,'letters':len(server.store.letters),
                      'stored_replies':[row['reply_text'] for row in server.store.letters]},ensure_ascii=True))
asyncio.run(main())
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith(('OLIVIA_', 'OPENAI_', 'DEEPSEEK_'))}
    env.update(OLIVIA_LOCAL_DATA_ROOT=str(tmp_path), OLIVIA_MEMORY_ENABLED='0',
               OLIVIA_PRIVATE_WORLD_ENABLED='false', OLIVIA_LLM_PROVIDER='openai_compatible',
               OLIVIA_LLM_BASE_URL='https://example.invalid/v1', OLIVIA_LLM_MODEL='synthetic',
               OLIVIA_LLM_REQUIRES_API_KEY='false', PYTHONUTF8='1', PYTHONPATH=str(ROOT))
    env['TEST_GAP'] = str(gap)
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload['letters'] == 2 + gap
    assert len(payload['calls']) == 2 + gap
    context = json.dumps(payload['calls'][-1], ensure_ascii=False)
    assert '只是我编的场景，不是真的' in context
    assert all(reply == '你已经问过七遍了，答案一直一样。' for reply in payload['stored_replies'])
    assert '你已经问过七遍了' not in context
    assert 'fact_recall' in context
    # The real send pipeline must carry the attitude contract to the provider.
    assert '核对、重复提问、纠正记忆本身不表示恶意、试探或自欺' in context
    assert '不同意见和拒绝' in context
    assert '未被说明的个人经历保持未知' in context
    assert '后续回合不等于又过一天' in context
