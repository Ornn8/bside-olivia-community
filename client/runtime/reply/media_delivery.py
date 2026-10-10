"""Typed publication facts; lyrics and renderer prose are never memory evidence."""
import hashlib
import json
import re
from datetime import datetime

_SUMMARIES = {'speech': '已完成语音回复', 'cover': '已完成翻唱回复', 'music': '已完成音乐回复',
              'daily_life': '已确认分享日常视频'}
_FIELDS = {'event_id', 'letter_id', 'reply_revision', 'reply_sha256', 'component',
           'presentation', 'occurred_at', 'source_audio_id', 'summary'}

MEDIA_EVIDENCE_MEANING = ('这是林离此前给对方说话、唱歌的往事，字段和编号仅供定位，回信直接说事情本身。'
    '日常视频记录的是已发生生活事件的分享；成品就绪不是送达，分享不产生新的生活事件，也不证明对方已观看。'
    'media_outcome区分已经发出的部分与尚未确认发出的部分；准备、试着或答应不等于做完。'
    '同一reply_source_id的分项属于同次回复，不表示文件数量。'
    '只知道这里写明的事：不知道对方是否听过、是否试唱过、音质及歌名缺失原因；未发出不表示林离主动扣下成品。'
    '歌词表达与现实约定分开；回应歌词中的邀请或调侃不需要另编行程、手续或过去的决定来解释。')


def delivery_outcome(letter):
    events = delivery_references(letter)
    if not events:
        return None  # Legacy status alone cannot establish component completion.
    completed = {event['component'] for event in events}
    expected = set(completed)
    mode = letter.get('reply_mode')
    if mode in {'voice_reply', 'spoken_video', 'musical_video', 'voice_song_video'}:
        expected.add('speech')
    material = letter.get('material') or {}
    if isinstance(material, dict) and material.get('cover_source_id'):
        expected.add('cover')
    elif mode in {'musical_video', 'voice_song_video', 'singing_video'}:
        expected.add('music')
    missing = ('这部分没成功，停在哪一步未知，不能确认已经做出成品' if letter.get('media_status') in {'FAILED', 'UNAVAILABLE'}
               else '尚不能确认已发给对方')
    return {component: '已经发给对方' if component in completed else missing
            for component in sorted(expected)}


def validate_delivery(value):
    daily = isinstance(value, dict) and value.get('component') == 'daily_life'
    if not isinstance(value, dict) or set(value) != (_FIELDS | {'daily_event', 'task_id', 'message_id'} if daily else _FIELDS):
        raise ValueError('MEDIA_DELIVERY_INVALID')
    if value['component'] not in _SUMMARIES or value['presentation'] not in {'audio', 'video'}:
        raise ValueError('MEDIA_DELIVERY_INVALID')
    if type(value['reply_revision']) is not int or value['reply_revision'] < 1:
        raise ValueError('MEDIA_DELIVERY_INVALID')
    for key in ('event_id', 'letter_id', 'reply_sha256', 'occurred_at'):
        if not isinstance(value[key], str) or not value[key] or len(value[key]) > 256:
            raise ValueError('MEDIA_DELIVERY_INVALID')
    if not re.fullmatch(r'[0-9a-f]{64}', value['reply_sha256']):
        raise ValueError('MEDIA_DELIVERY_INVALID')
    if datetime.fromisoformat(value['occurred_at']).utcoffset() is None:
        raise ValueError('MEDIA_DELIVERY_INVALID')
    source = value['source_audio_id']
    if source is not None and (value['component'] != 'cover' or not isinstance(source, str)
                              or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', source)):
        raise ValueError('MEDIA_DELIVERY_INVALID')
    identity = f"{value['letter_id']}:{value['reply_revision']}:{value['component']}"
    if value['event_id'] != 'media:' + hashlib.sha256(identity.encode()).hexdigest():
        raise ValueError('MEDIA_DELIVERY_INVALID')
    if value['summary'] != _SUMMARIES[value['component']]:
        raise ValueError('MEDIA_DELIVERY_INVALID')
    if daily:
        from runtime.personal_chat.daily_video import validate_input
        request = validate_input(value['daily_event'])
        digest = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        if (value['presentation'] != 'video' or value['reply_revision'] != 1
                or value['source_audio_id'] is not None or value['reply_sha256'] != digest
                or value['letter_id'] != 'daily-video:' + hashlib.sha256(request['event_id'].encode()).hexdigest()
                or not isinstance(value['task_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value['task_id'])
                or not isinstance(value['message_id'], str) or not 1 <= len(value['message_id']) <= 256
                or datetime.fromisoformat(value['occurred_at']) < datetime.fromisoformat(request['event_at'])):
            raise ValueError('MEDIA_DELIVERY_INVALID')
    return dict(value)


def make_daily_delivery(request, *, task_id, message_id, occurred_at):
    from runtime.personal_chat.daily_video import validate_input
    request = validate_input(request)
    letter_id = 'daily-video:' + hashlib.sha256(request['event_id'].encode()).hexdigest()
    identity = f'{letter_id}:1:daily_life'
    return validate_delivery(dict(event_id='media:' + hashlib.sha256(identity.encode()).hexdigest(),
        letter_id=letter_id, reply_revision=1,
        reply_sha256=hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest(),
        component='daily_life', presentation='video', occurred_at=occurred_at.isoformat(),
        source_audio_id=None, summary=_SUMMARIES['daily_life'], daily_event=request,
        task_id=task_id, message_id=message_id))


def make_delivery(letter, *, component, presentation, occurred_at):
    text = letter.get('reply_text')
    if not isinstance(text, str) or letter.get('letter_status') != 'COMPLETED':
        raise ValueError('MEDIA_DELIVERY_NOT_CANONICAL')
    digest = hashlib.sha256(text.encode()).hexdigest()
    if letter.get('private_world_reply_sha256') != digest:
        raise ValueError('MEDIA_DELIVERY_NOT_CANONICAL')
    revision = letter.get('reply_revision')
    identity = f"{letter.get('letter_id')}:{revision}:{component}"
    material = letter.get('material') or {}
    source = material.get('cover_source_id') if component == 'cover' and isinstance(material, dict) else None
    return validate_delivery(dict(event_id='media:' + hashlib.sha256(identity.encode()).hexdigest(),
        letter_id=letter.get('letter_id'), reply_revision=revision, reply_sha256=digest,
        component=component, presentation=presentation, occurred_at=occurred_at.isoformat(),
        source_audio_id=source, summary=_SUMMARIES[component]))


def delivery_references(letter):
    result = []
    values = letter.get('media_deliveries', [])
    if not isinstance(values, list):
        return result
    for value in values[:3]:
        try:
            event = validate_delivery(value)
        except (ValueError, TypeError, KeyError):
            continue
        if (event['letter_id'] == letter.get('letter_id')
                and event['reply_revision'] == letter.get('reply_revision')
                and event['reply_sha256'] == letter.get('private_world_reply_sha256')
                and event['reply_sha256'] == hashlib.sha256(str(letter.get('reply_text', '')).encode()).hexdigest()):
            result.append(event)
    return result


def delivery_evidence(event):
    """Keep binding hashes in storage rather than spending model context on them."""
    if event['component'] == 'daily_life':
        request = event['daily_event']
        return dict(source_id=event['event_id'], reply_source_id=request['event_id'],
            source_event_id=request['event_id'], component='daily_life', presentation='video',
            occurred_at=event['occurred_at'], event_at=request['event_at'], event_kind=request['event_kind'],
            summary='林离已通过QQ分享这件日常活动的视频。确认分享不证明对方已观看，也不产生新的生活事件。')
    action = {'speech': '说话', 'cover': '翻唱', 'music': '音乐'}[event['component']]
    presentation = '视频' if event['presentation'] == 'video' else '音频'
    return {'source_id': event['event_id'],
            'reply_source_id': f"reply:{event['letter_id']}:{event['reply_revision']}",
            **({'song_title': None, 'title_missing_reason': None} if event['component'] in {'cover', 'music'} else {}),
            'summary': f'林离发给对方的{presentation}回复中包含{action}内容',
            **{key: event[key] for key in
        ('component', 'presentation', 'occurred_at', 'source_audio_id')}}


def grouped_delivery_evidence(events):
    """One remembered reply with component facts, not an apparent file list."""
    groups = {}
    for event in events:
        evidence = delivery_evidence(event)
        source = evidence.pop('reply_source_id')
        component = evidence.pop('component')
        group = groups.setdefault(source, {'source_id': source, 'parts': {}})
        group['parts'][component] = evidence
    return list(groups.values())
