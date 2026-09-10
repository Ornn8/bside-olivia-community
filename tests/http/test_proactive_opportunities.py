import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

from runtime.reply.proactive_letters import scan_pending, write_json, make_context, settings


def test_background_only_prepares_one_current_intent_and_never_modifies_mailbox(tmp_path):
    rows = [{'letter_id': 'u1', 'letter_status': 'COMPLETED', 'content': '明天面试。',
             'reply_text': '好啊。', 'created_at': 1000, 'reply_revision': 1}]
    mailbox = tmp_path / 'state.json'
    mailbox.write_text(json.dumps({'letters': rows}), encoding='utf-8')
    before = mailbox.read_bytes()
    write_json(tmp_path / 'proactive/settings.json', {**settings({}), 'enabled': True})
    context = make_context(rows, now=2000)
    write_json(tmp_path / 'proactive/context.json', context)
    intent = scan_pending(tmp_path, now=3100)
    assert intent['source_id'] == 'reply:u1:1'
    assert 'prompt' not in intent and 'reply_text' not in intent
    assert scan_pending(tmp_path, now=3200) == intent
    assert mailbox.read_bytes() == before
    rows.append({'letter_id': 'p1', 'origin': 'proactive', 'letter_status': 'COMPLETED',
                 'proactive_candidate_id': intent['id'], 'published_at': 3200, 'is_read': 1})
    write_json(tmp_path / 'proactive/context.json', make_context(rows, now=3300))
    assert scan_pending(tmp_path, now=5000) == {}


def test_quota_unread_and_expiry_are_rechecked_without_catchup(tmp_path):
    prefs = {**settings({}), 'enabled': True}
    write_json(tmp_path / 'proactive/settings.json', prefs)
    rows = [{'letter_id': 'u1', 'letter_status': 'COMPLETED', 'content': '近况',
             'reply_text': '嗯', 'created_at': 1000}]
    for i in range(3):
        rows.append({'origin': 'proactive', 'letter_status': 'COMPLETED', 'published_at': 1100+i, 'is_read': 1})
    context = make_context(rows, now=2000)
    assert context['remaining'] == 0
    write_json(tmp_path / 'proactive/context.json', context)
    assert scan_pending(tmp_path, now=3200) == {}
    rows = rows[:1]
    write_json(tmp_path / 'proactive/context.json', make_context(rows, now=2000))
    assert scan_pending(tmp_path, now=2000 + 86400 * 8) == {}
    write_json(tmp_path / 'proactive/settings.json', settings({}))
    assert scan_pending(tmp_path, now=3100) == {}


@pytest.mark.parametrize('voice_ok', [False, True])
def test_publication_locks_send_hides_draft_and_commits_only_final_letter(tmp_path, voice_ok):
    script = r'''
import asyncio, json, time, os
from types import SimpleNamespace
import local_server as server
from runtime.reply.proactive_letters import write_json
root = server._state_root()
write_json(root / 'proactive/settings.json', {'enabled': True, 'allow_voice': True})
server.store.letters.clear()
server._proactive_ready = lambda: True
server.private_world_committer = None
committed = []
server._schedule_daily_life_exchange = lambda letter: committed.append(letter.copy())
async def complete(*args, **kwargs):
    assert server._proactive_busy
    response = await server.route('POST', '/toy/letter/send', {'content': '同时来信'}, {})
    assert response['code'] == 409, response
    return '我刚把那段谱子整理好了。'
server._proactive_complete = complete
async def render(lid, content, body, mode):
    assert content == '' and server._proactive_busy
    assert server._letter_collection('current') == []
    assert not committed
    row = server.store.letters[0]
    row['media_status'] = 'COMPLETED' if os.environ['VOICE_OK'] == '1' else 'FAILED'
    if row['media_status'] == 'COMPLETED':
        path = root / 'media' / (lid + '.wav')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'synthetic playable artifact')
        row['reply_audio_url'] = 'http://127.0.0.1/toy/media/' + path.name
        server._record_published_media(row, reply_text=body, delivery_id=row['private_world_delivery_id'],
                                       path=path, components=('speech',), presentation='audio')
        assert not row.get('media_deliveries')
server._render_media_job = render
async def main():
    await server._publish_proactive({'id': 'test', 'source_id': 'reply:u1:1'},
                                    {'format': 'voice', 'title': '写给你'})
    assert not server._proactive_busy
    assert len(committed) == 1
    row = server._letter_collection('current')[0]
    assert row['content'] == '' and row['origin'] == 'proactive'
    assert row['reply_mode'] == ('voice_reply' if os.environ['VOICE_OK'] == '1' else 'text')
    assert row['reply_revision'] == 1
    if os.environ['VOICE_OK'] == '1':
        assert len(row['media_deliveries']) == 1
    assert server.letter_to_out(row)['reply_allowed'] is False
    assert row['published_at'] <= time.time()
    assert row['private_world_status'] == 'PENDING'
    server.private_world_committer = SimpleNamespace(commit=lambda event: server.DeliveryStatus.COMMITTED)
    assert server.recover_pending_private_world() == 1
    assert row['private_world_status'] == 'COMMITTED'
    assert server.recover_pending_private_world() == 0
    detail = await server.route('GET', '/toy/letter/detail', {}, {'letter_id':row['letter_id']})
    assert detail['code'] == 0, detail
    assert json.loads((root / 'state.json').read_text(encoding='utf-8'))['letters'][0]['is_read'] == 1
    async def broken(*args, **kwargs):
        raise ValueError('bad model response')
    server._proactive_complete = broken
    try:
        await server._publish_proactive({'id':'second'}, {'format':'text','title':'x'})
    except ValueError:
        pass
    assert not server._proactive_busy and len(committed) == 1
    server.store.letters[:] = [{'letter_id':'u2', 'content':'明天面试', 'reply_text':'好啊',
                               'letter_status':'COMPLETED', 'created_at':time.time()-4000,
                               'reply_revision':1}]
    async def plan_with_new_user(*args, **kwargs):
        assert kwargs['planning'] and not server._proactive_busy
        server.store.letters.append({'letter_id':'u3', 'content':'面试结束了', 'letter_status':'PENDING'})
        return json.dumps({'decision':'send','format':'text','title':'面试怎么样'})
    server._proactive_complete = plan_with_new_user
    server._refresh_proactive_context()
    await server._proactive_tick()
    assert len(committed) == 1 and not server._proactive_busy
    assert server._proactive_reason == 'deferred'
    class UnavailableWorld:
        def __init__(self):
            self.store = self
            self.closed = False
        def exchange_state(self):
            raise OSError('synthetic unavailable database')
        def schedule_refresh(self, now):
            pass
        async def close(self):
            self.closed = True
    server.daily_life_runtime = UnavailableWorld()
    server.store.letters.clear()
    assert server._refresh_proactive_context()['blocked']
    await server._start_reply_tasks(None)
    await server._stop_reply_tasks(None)
    assert server.daily_life_runtime.closed and server._proactive_task is None
    result = await server.route('POST', '/toy/proactive/settings', {'enabled':False, 'allow_voice':False}, {},
                                companion_confirmed=True)
    assert result['code'] == 0 and result['data']['enabled'] is False
    rejected = await server.route('POST', '/toy/proactive/settings', {'enabled':'true'}, {},
                                  companion_confirmed=True)
    assert rejected['code'] == 400
asyncio.run(main())
print('verified')
'''
    root = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if not k.startswith(('OLIVIA_', 'OPENAI_', 'DEEPSEEK_'))}
    env.update(OLIVIA_LOCAL_DATA_ROOT=str(tmp_path), OLIVIA_MEMORY_ENABLED='0',
               OLIVIA_PRIVATE_WORLD_ENABLED='false', OLIVIA_LLM_PROVIDER='openai_compatible',
               OLIVIA_LLM_BASE_URL='https://example.invalid/v1', OLIVIA_LLM_MODEL='synthetic',
               OLIVIA_LLM_REQUIRES_API_KEY='false', PYTHONUTF8='1', PYTHONPATH=str(root))
    env['VOICE_OK'] = '1' if voice_ok else '0'
    result = subprocess.run([sys.executable, '-c', script], cwd=root, env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
