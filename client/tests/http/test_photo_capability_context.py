import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('enabled', [True, False])
def test_letter_writer_sees_frozen_photo_permission(monkeypatch, enabled):
    import local_server as server
    from reply_context import ReplyMode
    seen = []
    async def run(request, context):
        seen.append(context.to_dict())
        return object()
    monkeypatch.setattr(server, 'reply_pipeline', SimpleNamespace(run=run))
    monkeypatch.setattr(server.video_reply_settings_store, 'image_snapshot',
                        lambda: {'enabled': not enabled, 'resolution': '4K'})
    asyncio.run(server._run_reply_pipeline_for_letter(
        {'letter_id': 'photo-capability-fixture',
         'image_reply_settings': {'enabled': enabled, 'resolution': '1K'}},
        '请给我一张自拍', ReplyMode.TEXT_LETTER.value, idempotency_key='fixture'))
    facts = [f for f in seen[0]['world_facts'] if f['fact_id'] == 'runtime.photo_attachment']
    assert len(facts) == 1
    assert ('已开启' if enabled else '未开启') in facts[0]['statement']
    if not enabled:
        assert '本渠道支持发送照片' in facts[0]['statement']
        assert '回信格式设置' in facts[0]['statement']


@pytest.mark.parametrize('channel,mode,available', [
    ('qq', 'future_im', True), ('wechat', 'future_im', False),
    ('letter', 'singing_video', False), ('letter', 'voice_reply', True)])
def test_photo_capability_matches_supported_routes(channel, mode, available):
    from runtime.image_reply import photo_reply_context
    from reply_context import ReplyContext, ReplyMode, TrustedTime
    original = ReplyContext.create(ReplyMode(mode), future_im_enabled=True,
                                  trusted_time=TrustedTime(datetime.now(timezone.utc)))
    result = photo_reply_context(original, {'enabled': True}, channel=channel)
    assert not original.world_facts
    assert ('已开启' if available else '未开启') in result.world_facts[-1].statement
    assert photo_reply_context(result, {'enabled': True}, channel=channel) == result
