import ast
import asyncio
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from runtime.private_world.life_rhythm import bath_window, rhythm
from original_client_letter_contract import serialize_letter_summary
from patch_companion_settings import _repair_native_letter_audio


@pytest.mark.parametrize('stamp,phase', [
    ('2026-09-10T23:30:00+08:00', 'quiet'),
    ('2026-09-10T23:59:59+08:00', 'quiet'),
    ('2026-09-11T00:00:00+08:00', 'sleep'),
    ('2026-09-11T08:29:59+08:00', 'sleep'),
    ('2026-09-11T08:30:00+08:00', 'breakfast'),
    ('2026-09-12T08:30:00+08:00', 'breakfast'),
])
def test_sleep_boundaries(stamp, phase):
    value = rhythm(datetime.fromisoformat(stamp), [])
    assert value['phase'] == phase
    assert value['bath_end_at'] is None


@pytest.mark.parametrize('seed', ['', 'install-a', 'install-b'])
def test_bath_window_boundaries(seed):
    begin, end = bath_window(date(2026, 9, 10), seed)
    second = timedelta(seconds=1)
    assert rhythm(begin - second, [], bath_seed=seed)['phase'] != 'bathing'
    for stamp in (begin, end - second):
        value = rhythm(stamp, [], bath_seed=seed)
        assert value['phase'] == 'bathing' and value['availability'] == 'rest'
        assert value['bath_end_at'] == end.timestamp()
    assert rhythm(end, [], bath_seed=seed)['phase'] != 'bathing'


def test_bath_is_an_evening_of_15_to_30_minutes_and_differs_by_install():
    days = [date(2026, 9, 1) + timedelta(days=n) for n in range(60)]
    for seed in ('', 'install-a'):
        for day in days:
            begin, end = bath_window(day, seed)
            assert begin.date() == day
            assert time(20) <= begin.timetz().replace(tzinfo=None) <= time(22, 30)
            assert timedelta(minutes=15) <= end - begin <= timedelta(minutes=30)
    assert len({bath_window(day, '')[0].time() for day in days}) > 10
    assert len({end - begin for begin, end in (bath_window(day, '') for day in days)}) > 5
    same = sum(bath_window(day, 'install-a')[0] == bath_window(day, 'install-b')[0] for day in days)
    assert same < 10


def test_install_keeps_its_bath_seed(tmp_path):
    from runtime.private_world.daily_life import DailyLifeStore
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    again = DailyLifeStore(tmp_path / 'life.sqlite3')
    other = DailyLifeStore(tmp_path / 'other.sqlite3')
    assert store.bath_seed == again.bath_seed != other.bath_seed
    begin, _ = store.bath_window(date(2026, 9, 10))
    assert store.snapshot(begin)['rhythm']['phase'] == 'bathing'


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
