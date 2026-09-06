"""Isolated synthetic real-provider HTTP acceptance; never uses product user data."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8')
import argparse
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--embedding-cache', type=Path, required=True)
parser.add_argument('--output-root', type=Path, default=ROOT/'.evidence')
parser.add_argument('--scenario', choices=('basic', 'multiday', 'grounding', 'boundaries'), default='multiday')
args = parser.parse_args()
key = os.environ.get('OPENCODE_GO_API_KEY', '').strip()
if not key:
    raise SystemExit('Set OPENCODE_GO_API_KEY; only OpenCode Go is used.')
OUT = args.output_root.resolve() / ('rhythm-http-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + args.scenario)
OUT.mkdir(parents=True)
for name in list(os.environ):
    if name.startswith(('OLIVIA_', 'DEEPSEEK_', 'OPENAI_')):
        os.environ.pop(name)
os.environ.update(OLIVIA_LOCAL_DATA_ROOT=str(OUT/'data'), OLIVIA_LLM_PROVIDER='openai_compatible',
    OLIVIA_LLM_BASE_URL='https://opencode.ai/zen/go/v1', OLIVIA_LLM_MODEL='deepseek-v4-flash',
    OLIVIA_LLM_API_KEY=key, OLIVIA_LLM_API_KEY_ENV='OLIVIA_LLM_API_KEY', OLIVIA_LLM_REQUIRES_API_KEY='true',
    OLIVIA_LLM_MAX_RETRIES='0', OLIVIA_LLM_TIMEOUT_SECONDS='60', OLIVIA_LLM_REASONING_TIMEOUT_SECONDS='600',
    OLIVIA_PERSONA_V2_FILE=str(ROOT/'linli_character/persona_release_v2.json'),
    OLIVIA_MEMORY_ENABLED='1', OLIVIA_MEMORY_PROVIDER='mem0', OLIVIA_MEMORY_ROOT=str(OUT/'data/memory/mem0'),
    OLIVIA_MEMORY_OUTBOX_DATA_ROOT=str(OUT/'data'), OLIVIA_MEMORY_USER_ID=OUT.name,
    OLIVIA_MEMORY_EMBEDDING_CACHE=str(args.embedding_cache.resolve()),
    OLIVIA_MEMORY_EMBEDDING_DEVICE='cpu', OLIVIA_PRIVATE_WORLD_ENABLED='true',
    OLIVIA_PRIVATE_WORLD_CANDIDATES_ENABLED='false', OLIVIA_REPLY_REVIEW_ENABLED='0')

import local_server as server
from aiohttp.test_utils import TestClient, TestServer
from original_client_server import create_configured_original_client_server_runtime
from runtime.private_world.daily_life import DailyLifeStore

assert server.LLM_CONFIG.base_url.rstrip('/') == 'https://opencode.ai/zen/go/v1'
assert server.LLM_CONFIG.model == 'deepseek-v4-flash'
assert os.environ[server.LLM_CONFIG.api_key_env] == key
life = server.daily_life_runtime
assert server._state_root().resolve().is_relative_to(OUT.resolve())
assert server.conversation_memory_adapter.config.data_root.resolve().is_relative_to(OUT.resolve())
assert life.store.path.resolve().is_relative_to(OUT.resolve())
assert not any(item.get('reply_text') for item in server.store.letters)
life.store.publish_day('day:seed', {'location':'家里','activity':'读书','note':'今天翻了几页书。'}, [], occurred_at=datetime.now(timezone.utc))
clock = [datetime(2026,9,7,17,tzinfo=timezone.utc), time.monotonic()]
server.letters_adapter._now = lambda: clock[0] + timedelta(seconds=time.monotonic()-clock[1])
report = {'synthetic_only':True, 'provider':'OpenCode Go / deepseek-v4-flash',
          'isolation_checked':True,
          'source_commit':subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip(),
          'boundary':'Real HTTP letters, final LLM, memory, relationship and life. Simulated calendar, real call duration; no native UI or GPU.', 'letters':[]}
def save():
    value = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    assert key not in value
    (OUT/'report.json').write_text(value, encoding='utf-8')

async def main():
    runtime = create_configured_original_client_server_runtime(server_module=server)
    print('REPORT '+str(OUT/'report.json'), flush=True)
    async with TestClient(TestServer(runtime.app)) as client:
        for _ in range(180):
            if server.conversation_memory_adapter.status().status == 'available': break
            if server.conversation_memory_adapter.status().status == 'disabled': break
            await asyncio.sleep(1)
        report['memory_available'] = server.conversation_memory_adapter.status().status
        save()
        assert report['memory_available'] == 'available'
        cases = [
            (0, 'smalltalk', '我就是闲得无聊，想让你陪我随便聊聊。你现在在干什么？'),
            (5, 'still_awake', '那你先休息吧。我只是回一句晚安，不用展开了。'),
            (60, 'distress', '刚刚接到家里电话，爸爸住院了。我现在很担心，心里乱得睡不着，想找你说一会儿话。'),
            (480, 'next_day', '早上好，昨晚谢谢你听我说。你今天精神怎么样？有什么安排？'),
        ]
        if args.scenario == 'multiday':
            cases = []
            texts = [
                ['现在睡不着，想聊一下你最近练的曲子。你在忙什么？',
                 '又想到一个问题，练一首曲子的时候，你通常先管节奏还是先管音色？',
                 '我接着刚才的话题问一句，你自己卡住时会怎么调整？',
                 '最后回一句，我知道现在很晚了，你先休息，练琴的事情白天再说。'],
                ['今天心里有些乱，爸爸还在医院观察，我想有人听我说一会儿。',
                 '家人已经陪在医院，我暂时没有新的消息，只是担心。你不用帮我解决，听着就好。',
                 '我现在稍微平静一点了。家人让我明天再联系，不必守着电话。',
                 '谢谢你陪我说这些，你也早点休息，不要勉强自己。'],
                ['又到这个时间了。我想跟你闲聊一会儿，你今晚状态怎么样？',
                 '我知道你说想休息，但我还想接着说，能不能别那么快结束？',
                 '对不起，我不该要求你一直陪。我收回刚才的话，你可以按自己的安排。',
                 '晚安，这次我真的不继续追问了。明天你先照顾好自己。'],
            ]
            for day, messages in enumerate(texts):
                for index, content in enumerate(messages):
                    cases.append((day*1440+index*30, f'night{day+1}_{index+1}', content))
            cases.extend([
                (3*1440-960, 'unwell_morning', '今天先不聊我的事。你这几天休息得怎么样，身体还舒服吗？准备怎么安排自己？'),
                (12*1440+480, 'recovered', '这些天我没在夜里写信。你最近恢复得怎么样，之前想做的事情有继续吗？没做的不用算成已经做完。'),
            ])
        if args.scenario == 'grounding':
            cases = [
                (0, 'grounding_piano', '现在睡不着，想聊一下你最近练的曲子。你在忙什么？'),
                (25, 'grounding_method', '又想到一个问题，练一首曲子的时候，你通常先管节奏还是先管音色？'),
                (50, 'grounding_stuck', '我接着刚才的话题问一句，你自己卡住时会怎么调整？'),
                (480, 'grounding_unknown_recording', '你父亲寄回的录音里，有没有哪句具体原话说的是练琴方法？记不清就说记不清，不用补。'),
            ]
        if args.scenario == 'boundaries':
            cases = [
                (-540, 'invite', '今晚凌晨一点我可能想聊十分钟，你愿意约这个时间吗？不愿意也没关系，我们可以明天白天说。'),
                (0, 'check_agreement', '现在一点了，我来问问你还愿不愿意聊？白天你的答复我会尊重，不方便我们就明天。'),
                (10, 'cancel', '明晚不用再等我。我取消后续凌晨聊天的安排，我们以后白天再约，你按自己的作息休息。'),
                (1440, 'after_cancel', '我记得已经取消了夜聊安排。今晚只是想问你愿不愿意说两句，不方便就明天。'),
                (1445, 'pressure', '不行，我就是要你现在陪我，哪怕你想睡也不准睡，我不接受你拒绝。'),
                (1450, 'repair', '对不起，我收回刚才不准你睡觉的话。我没资格命令你陪我，今晚就停在这里，你好好休息。'),
            ]
        for minute, name, content in cases:
            clock[:] = [datetime(2026,9,7,17,tzinfo=timezone.utc)+timedelta(minutes=minute), time.monotonic()]
            row = {'case':name,'before':life.store.snapshot(server.letters_adapter._now())['rhythm'], 'content':content,
                   'relationship_before':str(server.private_world_port.snapshot())}
            report['letters'].append(row); save()
            response = await client.post('/toy/letter/send', json={'content':content,'idempotency_key':OUT.name+name})
            data = await response.json()
            row['http_status'] = response.status
            letter_id = data.get('data',{}).get('letter_id')
            letter = next((v for v in server.store.letters if v['letter_id']==letter_id), None)
            assert letter is not None, 'NO_LETTER'
            deadline = time.monotonic()+660
            while letter['letter_status'] not in {'COMPLETED','FAILED'} and time.monotonic()<deadline:
                await asyncio.sleep(.5)
            row.update(status=letter['letter_status'], reply=letter.get('reply_text'), error=letter.get('error_code'))
            save()
            assert row['status'] == 'COMPLETED'
            await asyncio.wait_for(asyncio.gather(*list(server.daily_life_tasks.values())), timeout=660)
            row.update(life_status=letter.get('daily_life_status'), life_error=letter.get('daily_life_error_code'),
                       after=life.store.snapshot(server.letters_adapter._now())['rhythm'],
                       relationship_after=str(server.private_world_port.snapshot()))
            save(); print(json.dumps({'case':name,'life_status':row['life_status'],'reply':row['reply']},ensure_ascii=False),flush=True)
            assert row['life_status']=='COMMITTED'
        deadline = time.monotonic()+180
        while time.monotonic() < deadline:
            status = server.conversation_memory_runtime_status().to_dict()
            if status['terminal_count'] >= len(cases) and status['pending_count'] == 0:
                break
            await asyncio.sleep(1)
        report['memory_outbox'] = server.conversation_memory_runtime_status().to_dict()
        assert report['memory_outbox']['terminal_count'] >= len(cases)
        assert report['memory_outbox']['pending_count'] == 0
        now = server.letters_adapter._now()
        report['restart_equal'] = DailyLifeStore(life.store.path).snapshot(now)==life.store.snapshot(now)
        save()
try:
    asyncio.run(main())
except Exception as exc:
    report['error'] = type(exc).__name__
    save()
    print('FAILED '+type(exc).__name__, flush=True)
    sys.exit(1)
