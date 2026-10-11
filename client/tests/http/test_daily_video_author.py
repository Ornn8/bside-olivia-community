import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from runtime.personal_chat.daily_video_author import author_preparation
from llm_gateway import GatewayRequestScope


def test_preparation_uses_main_writer_once_with_bounded_persona_world_and_no_chat_send():
    candidate = dict(version=1, event_id='bath:started:synthetic', target_date='2026-10-06',
        event_at=datetime(2026, 10, 6, 12, tzinfo=timezone.utc).isoformat(),
        event_kind='bath_finished', scene_id='bathroom', certainty='live', event_status='preparing')
    selection = dict(event_id=candidate['event_id'], spoken_text='洗完了，头发还湿着。',
        share_text='这是洗完澡那会儿的样子。', staging=dict(image_direction='A relaxed close selfie.',
        video_direction='A small glance back toward the lens.'))
    calls = []
    async def complete(messages, **kwargs):
        calls.append((messages, kwargs))
        return SimpleNamespace(text=json.dumps({'daily_video': selection}, ensure_ascii=False))
    gateway = SimpleNamespace(config=SimpleNamespace(max_input_chars=30000), complete_structured_scoped=complete)
    runtime = SimpleNamespace(gateway=lambda: gateway, persona=lambda: 'persona' * 2000,
        store=SimpleNamespace(reply_context=lambda *a, **kw: 'Known world and remembered relationship.'))
    answer = asyncio.run(author_preparation(SimpleNamespace(daily_life_runtime=runtime), candidate))
    assert len(calls) == 1 and calls[0][1]['scope'] == GatewayRequestScope.PERSONAL_CHAT_JSON
    packet = json.loads(calls[0][0][1]['content'])
    assert len(packet['persona']) == 6000 and packet['daily_video_candidates'] == [candidate]
    assert packet['world_and_memory'] == 'Known world and remembered relationship.'
    assert answer['spoken_text'] == selection['spoken_text'] and answer['share_text'] == selection['share_text']
    assert answer['event_at'] == candidate['event_at'] and answer['certainty'] == 'live'
    assert 'event_status=preparing' in calls[0][0][0]['content']
    assert 'event_status' not in answer


def test_unavailable_writer_does_not_fall_back_to_jev():
    with pytest.raises(ValueError, match='DAILY_VIDEO_AUTHOR_UNAVAILABLE'):
        asyncio.run(author_preparation(SimpleNamespace(daily_life_runtime=None), {}))
