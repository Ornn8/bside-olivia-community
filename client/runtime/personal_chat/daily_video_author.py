"""One main-writer call for an actual started event when chat has not authored it."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json

from llm_gateway import GatewayRequestScope
from .decision import INSTRUCTION
from .daily_video import select_candidate


async def author_preparation(server, candidate):
    return await _author(server, candidate, purpose='daily-video-author',
        query=candidate.get('detail', '')[:180] + ' 洗澡前后分享短视频',
        note='这是日常视频的后台准备，不是用户新发言。'
             '只为提供的已实际开始事件编排，必须在本次输出中选择daily_video，完成台词、staging和share_text。'
             '预先准备完成后的样子，不把计划当作完成事实；不设新约定、不改变偏好。text仅供解析，不发送。')


async def author_share(server, candidate, row):
    """Her words for a short clip she sends right after this reply, in place of a photo."""
    return await _author(server, candidate, purpose='daily-video-share',
        query=candidate.get('detail', '')[:180] + ' 分享此刻的小视频',
        conversation=dict(user=str(row.get('content') or '')[:600], her_reply=str(row.get('reply_text') or '')[:600]),
        note='这是她刚回复对方之后，顺手录给对方的一段十几秒小视频，不是用户新发言。'
             '必须在本次输出中选择daily_video：spoken_text是她对着镜头自然说的一两句，接着conversation里刚才的聊天，'
             '不重复回信原话；同时完成staging和share_text。不设新约定、不改变偏好。text仅供解析，不发送。')


async def _author(server, candidate, *, purpose, query, note, conversation=None):
    runtime = getattr(server, 'daily_life_runtime', None)
    if runtime is None:
        raise ValueError('DAILY_VIDEO_AUTHOR_UNAVAILABLE')
    gateway = runtime.gateway()
    structured = getattr(gateway, 'complete_structured_scoped', None)
    if structured is None:
        raise ValueError('DAILY_VIDEO_AUTHOR_UNAVAILABLE')
    now = datetime.now(timezone.utc)
    persona = runtime.persona()
    persona = persona if isinstance(persona, str) else json.dumps(persona, ensure_ascii=False)
    packet = dict(channel='qq', decision_now=now.isoformat(), daily_video_candidates=[candidate],
        persona=persona[:6000], world_and_memory=runtime.store.reply_context(query, now=now, max_chars=4000),
        **({'conversation': conversation} if conversation else {}))
    messages = (
        dict(role='system', content=INSTRUCTION + '\n' + note),
        dict(role='user', content=json.dumps(packet, ensure_ascii=False, separators=(',', ':'))),
    )
    if sum(len(message['content']) for message in messages) > getattr(getattr(gateway, 'config', None), 'max_input_chars', 30000):
        raise ValueError('DAILY_VIDEO_AUTHOR_CONTEXT_TOO_LARGE')
    result = await asyncio.wait_for(structured(messages,
        request_id=purpose + ':' + hashlib.sha256(candidate['event_id'].encode()).hexdigest(),
        scope=GatewayRequestScope.PERSONAL_CHAT_JSON, response_format={'type': 'json_object'}), timeout=240)
    try:
        if not isinstance(result.text, str) or len(result.text) > 12000:
            raise ValueError()
        selected = select_candidate(json.loads(result.text)['daily_video'], [candidate])
        if not {'staging', 'share_text'} <= selected.keys():
            raise ValueError()
        return selected
    except (ValueError, TypeError, KeyError):
        raise ValueError('DAILY_VIDEO_AUTHOR_INVALID') from None
