import ast
import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from runtime.private_world.life_rhythm import rhythm
from original_client_letter_contract import serialize_letter_summary
from patch_companion_settings import _repair_native_letter_audio


@pytest.mark.parametrize('stamp,phase', [
    ('2026-09-10T23:29:59+08:00', 'quiet'),
    ('2026-09-10T23:30:00+08:00', 'bathing'),
    ('2026-09-10T23:59:59+08:00', 'bathing'),
    ('2026-09-11T00:00:00+08:00', 'sleep'),
    ('2026-09-11T08:29:59+08:00', 'sleep'),
    ('2026-09-11T08:30:00+08:00', 'breakfast'),
    ('2026-09-12T08:30:00+08:00', 'breakfast'),
])
def test_bath_and_sleep_boundaries(stamp, phase):
    value = rhythm(datetime.fromisoformat(stamp), [])
    assert value['phase'] == phase
    assert bool(value['bath_end_at']) == (phase == 'bathing')


def test_recovered_bath_letter_waits_then_replies_once_without_memory_timeout():
    source = Path(__file__).resolve().parents[2] / 'local_server.py'
    names = {'_run_reply_when_memory_ready', '_schedule_reply_job'}
    nodes = [n for n in ast.parse(source.read_text(encoding='utf-8')).body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    clock = [100.0]
    # Same persisted fields used after restart; never store a coroutine/timer.
    letter = json.loads(json.dumps(dict(letter_id='bath', content='你好', created_at=100,
        letter_status='PENDING', reply_resume_at=1900, reply_wait_reason='bathing')))
    calls = []
    async def sleep(seconds):
        assert not calls
        clock[0] += seconds
        await asyncio.sleep(0)
    async def generate(lid, content, **kwargs):
        assert clock[0] >= 1900
        calls.append(lid)
        letter['letter_status'] = 'COMPLETED'
        return True
    namespace = dict(store=SimpleNamespace(letters=[letter]), time=SimpleNamespace(time=lambda: clock[0]),
        asyncio=SimpleNamespace(sleep=sleep, get_running_loop=asyncio.get_running_loop,
                                create_task=asyncio.create_task, Task=asyncio.Task),
        MEMORY_READY_REPLY_TIMEOUT_SECONDS=30, _conversation_memory_ready_for_reply=lambda: True,
        _run_reply_job=generate, reply_jobs={}, reply_tasks=set())
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    async def run():
        namespace['_schedule_reply_job']('bath', '你好', idempotency_key='one')
        namespace['_schedule_reply_job']('bath', '你好', idempotency_key='one')
        assert len(namespace['reply_jobs']) == 1
        assert serialize_letter_summary(letter, now=100)['replyWaitReason'] == 'bathing'
        await asyncio.gather(*namespace['reply_tasks'])
        assert 'replyWaitReason' not in serialize_letter_summary(letter, now=1900)
    asyncio.run(run())
    assert calls == ['bath']


def test_native_wait_caption_keeps_cover_and_audio_props_on_repeat_patch():
    source = '''const row={letterStatus:e.letterStatus,auditStatus:e.auditStatus};const view={__name:"MailBoxReplyContent",props:{videoPending:{type:Boolean},modelValue:{}}};F(ks,{key:1,videoPending:i.mail.videoPending},null,8,["modelValue","videoUrl","timestamp","type"]);v(A.videoPending?"林离录视频中":o(i)("mailbox_waiting_for_reply"));re.isUnread!==Ee.isUnread'''
    source += ';[A.type==="error"?1:0]'
    patched = _repair_native_letter_audio(source)
    assert 'replyWaitReason:e.replyWaitReason' in patched
    assert 'replyWaitReason:i.mail.replyWaitReason' in patched
    assert 'props:{replyWaitReason:{},coverId:{},audioUrl:{}' in patched
    assert 'A.replyWaitReason==="bathing"?"林离洗澡中"' in patched
    assert 'coverId:i.mail.coverId' in patched
    assert _repair_native_letter_audio(patched) == patched
