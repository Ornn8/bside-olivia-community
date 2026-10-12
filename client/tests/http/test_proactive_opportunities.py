import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

from runtime.reply.proactive_letters import scan_pending, write_json, make_context, settings


def test_background_only_prepares_one_current_intent_and_never_modifies_mailbox(tmp_path):
    rows = [{'letter_id': 'u1', 'letter_status': 'COMPLETED', 'content': '明天面试。',
             'reply_text': '好啊。', 'created_at': 1000, 'reply_revision': 1,
             'initiative_tier': 'close', 'initiative_caution': 'normal'}]
    mailbox = tmp_path / 'state.json'
    mailbox.write_text(json.dumps({'letters': rows}), encoding='utf-8')
    before = mailbox.read_bytes()
    write_json(tmp_path / 'proactive/settings.json', {**settings({}), 'enabled': True})
    context = make_context(rows, now=2000)
    write_json(tmp_path / 'proactive/context.json', context)
    intent = scan_pending(tmp_path, now=6500)
    assert intent['source_id'] == 'reply:u1:1'
    assert intent['relationship_tier'] == 'close'
    assert 'prompt' not in intent and 'reply_text' not in intent
    assert scan_pending(tmp_path, now=6600) == intent
    assert mailbox.read_bytes() == before
    rows.append({'letter_id': 'p1', 'origin': 'proactive', 'letter_status': 'COMPLETED',
                 'proactive_candidate_id': intent['id'], 'published_at': 6600, 'is_read': 1})
    write_json(tmp_path / 'proactive/context.json', make_context(rows, now=6700))
    assert scan_pending(tmp_path, now=10000) == {}


def test_relationship_profile_changes_letter_budget_and_reason_threshold():
    base = {'letter_id': 'u1', 'letter_status': 'COMPLETED', 'content': '近况',
            'reply_text': '嗯', 'created_at': 1000}
    reserved = make_context([dict(base)], now=2000)
    close = make_context([{**base, 'initiative_tier': 'close', 'initiative_caution': 'normal'}], now=2000)
    committed = make_context([{**base, 'initiative_tier': 'committed', 'initiative_caution': 'normal'}], now=2000)
    assert reserved['remaining'] == 1 and reserved['initiative_profile']['tier'] == 'reserved'
    assert close['remaining'] == 2 and committed['remaining'] == 6
    reserved_followup = next(item for item in reserved['candidates'] if item['kind'] == 'correspondence_followup')
    close_followup = next(item for item in close['candidates'] if item['kind'] == 'correspondence_followup')
    committed_followup = next(item for item in committed['candidates'] if item['kind'] == 'correspondence_followup')
    assert reserved_followup['not_before'] > close_followup['not_before'] > committed_followup['not_before']
    assert any(item['kind'] == 'relationship_checkin' for item in reserved['candidates'])
    assert any(item['kind'] == 'relationship_checkin' for item in close['candidates'])
    assert any(item['kind'] == 'relationship_checkin' for item in committed['candidates'])


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


@pytest.mark.parametrize('planned', ['text', 'voice'])
def test_publication_locks_send_hides_draft_and_commits_only_final_letter(tmp_path, planned):
    script = r'''
import asyncio, json, time, os
from datetime import datetime, timezone
from types import SimpleNamespace
import local_server as server
from runtime.reply.proactive_letters import scan_pending, write_json
root = server._state_root()
write_json(root / 'proactive/settings.json', {'enabled': True, 'allow_voice': True})
assert server._proactive_reason == 'disabled'
assert server._proactive_status()['reason'] == 'waiting'
server._proactive_reason = 'considering'
write_json(root / 'proactive/settings.json', {'enabled': False})
assert server._proactive_status()['reason'] == 'disabled'
write_json(root / 'proactive/settings.json', {'enabled': True, 'allow_voice': True})
server.store.letters.clear()
server.store.letters.append({'letter_id':'u1', 'content':'之前说起谱子', 'reply_text':'好啊',
                            'letter_status':'COMPLETED', 'created_at':time.time()-7200,
                            'reply_revision':1, 'is_read':1, 'initiative_tier':'close',
                            'initiative_caution':'normal'})
world = {}
server.daily_life_runtime = SimpleNamespace(store=SimpleNamespace(
    exchange_state=lambda: world, record_media_delivery=lambda event: False))
server._proactive_ready = lambda: True
server.private_world_committer = None
committed = []
server._schedule_daily_life_exchange = lambda letter: committed.append(letter.copy())
async def complete(*args, **kwargs):
    assert server._proactive_busy
    return '我刚把那段谱子整理好了。'
server._proactive_complete = complete
async def render(*args, **kwargs):
    raise AssertionError('A letter she starts on her own is never rendered as media.')
server._render_media_job = render
async def main():
    server._refresh_proactive_context()
    intent = scan_pending(root)
    assert intent.get('kind') == 'correspondence_followup'
    turn = await server._prepare_proactive_turn(intent, now=datetime.now(timezone.utc))
    await server._publish_proactive(intent, {'format': os.environ['PLANNED'], 'title': '写给你'}, turn=turn)
    assert not server._proactive_busy
    assert len(committed) == 1
    row = next(row for row in server._letter_collection('current') if row.get('origin') == 'proactive')
    assert row['content'] == '' and row['origin'] == 'proactive'
    assert row['reply_mode'] == 'text' and row['media_status'] == 'NOT_REQUESTED'
    assert row['reply_revision'] == 1
    assert not row.get('reply_audio_url') and not row.get('media_deliveries')
    assert not row.get('image_status') and not row.get('gift_photo')
    assert server.letter_to_out(row)['reply_allowed'] is False
    assert row['published_at'] <= time.time()
    assert row['private_world_status'] == 'PENDING'
    server.private_world_committer = SimpleNamespace(commit=lambda event: server.DeliveryStatus.COMMITTED)
    assert server.recover_pending_private_world() == 1
    assert row['private_world_status'] == 'COMMITTED'
    assert server.recover_pending_private_world() == 0
    detail = await server.route('GET', '/toy/letter/detail', {}, {'letter_id':row['letter_id']})
    assert detail['code'] == 0, detail
    persisted = json.loads((root / 'state.json').read_text(encoding='utf-8'))['letters']
    assert next(item for item in persisted if item.get('origin') == 'proactive')['is_read'] == 1
    async def text_letter(*args, **kwargs):
        return '第一段。\n\n\n第二段。\n[[signature:阿离]]'
    server._proactive_complete = text_letter
    world['shared'] = [{'id':'signature-followup', 'actor':'user', 'status':'planned',
        'source_id':'reply:u1:1',
        'updated_at':datetime.fromtimestamp(time.time()-2*86400, timezone.utc).isoformat()}]
    server._refresh_proactive_context()
    intent = scan_pending(root)
    assert intent.get('kind') == 'shared_followup'
    turn = await server._prepare_proactive_turn(intent, now=datetime.now(timezone.utc))
    await server._publish_proactive(intent, {'format':'text','title':'写给你'}, turn=turn)
    signed = server.store.letters[-1]
    assert signed['reply_text'] == '第一段。\n\n\n第二段。'
    assert signed['reply_signature'] == '阿离'
    assert committed[-1]['reply_text'] == signed['reply_text']
    from original_client_letter_contract import serialize_letter_detail
    assert serialize_letter_detail(signed)['replyText'] == signed['reply_text'] + '\n\n阿离'
    assert serialize_letter_detail(signed)['replyText'].count('阿离') == 1
    server.store.letters.pop()
    committed.pop()
    async def broken(*args, **kwargs):
        raise ValueError('bad model response')
    server._proactive_complete = broken
    try:
        server._refresh_proactive_context()
        intent = scan_pending(root)
        assert intent.get('kind') == 'shared_followup'
        turn = await server._prepare_proactive_turn(intent, now=datetime.now(timezone.utc))
        await server._publish_proactive(intent, {'format':'text','title':'x'}, turn=turn)
    except ValueError:
        pass
    assert not server._proactive_busy and len(committed) == 1
    server.store.letters[:] = [{'letter_id':'u2', 'content':'明天面试', 'reply_text':'好啊',
                               'letter_status':'COMPLETED', 'created_at':time.time()-7200,
                               'reply_revision':1, 'initiative_tier':'close',
                               'initiative_caution':'normal'}]
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
    assert result['data']['reason'] == 'disabled'
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
    env['PLANNED'] = planned
    result = subprocess.run([sys.executable, '-c', script], cwd=root, env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
