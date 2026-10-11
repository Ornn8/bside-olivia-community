"""Shared delivered conversation window for every reply channel."""
import json
from datetime import datetime, timezone, timedelta
from heapq import nlargest
from itertools import islice

from runtime.reply.recent_correspondence import recent_correspondence
from runtime.reply.fact_attribution import FACT_ATTRIBUTION_BOUNDARY
from runtime.reply.media_delivery import delivery_references, grouped_delivery_evidence, delivery_outcome

LOCAL = timezone(timedelta(hours=8))
# Chat windows start on multiples of this many exchanges, so the dialogue that
# opens the prompt stays identical for several turns and keeps hitting the cache.
WINDOW_STEP = 4
# Lower bound for the mandatory JSON keys of one dialogue row (without values
# or packet overhead). Enough candidates for any suffix that can fit, plus the
# stable-cache alignment. Archive retrieval below still sees all originals.
_ROW_KEYS_COST = sum(len(key) + 4 for key in ('source_id', 'channel', 'origin', 'received_at',
                                           'sent_at', 'replied_at', 'user_letter', 'linli_reply'))


def _time(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.astimezone(LOCAL).isoformat() if stamp.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def conversation_context(rows, *, query, now, excluded_sources=(), max_chars=6000):
    from runtime.personal_chat.context import READ_WINDOW
    frozen = READ_WINDOW.get()
    rows = list(rows) if frozen is None else frozen
    def delivered(row):
        if row.get('read_only') or not isinstance(row.get('letter_id'), str):
            return False
        if row.get('channel') in {'qq', 'wechat'}:
            return row.get('delivery_status') == 'DELIVERED'
        return row.get('letter_status') == 'COMPLETED'
    def eligible(row):
        return ((delivered(row) or frozen is not None and row.get('_received_only'))
                and not any(s.startswith(f"reply:{row['letter_id']}:") for s in excluded_sources))
    limit = max(1, max_chars // _ROW_KEYS_COST + WINDOW_STEP)
    if frozen is not None:
        # Already in authoritative receive/read order. Inspect only a bounded
        # tail, not a second full copy and sort of the archive.
        candidates = list(islice((r for r in reversed(rows) if eligible(r)), limit))
    else:
        # Legacy rows can have neither timestamp nor receipt sequence. Preserve
        # their input position as the final tie-breaker, just like stable sort.
        chosen = nlargest(limit, ((i, r) for i, r in enumerate(rows) if eligible(r)),
            key=lambda item: (item[1].get('_read_order', float(item[1].get('created_at', 0))),
                              item[1].get('received_sequence', 0), item[0]))
        candidates = [r for _, r in chosen]
    candidates.reverse()
    packet = {'kind': 'recent_dialogue', 'current_time': now.astimezone(LOCAL).isoformat(), 'timezone': 'Asia/Shanghai',
              'meaning': '以下是按接收顺序排列的最近连续交流，信件和聊天跨渠道仍是同一个人；保留各渠道，不把聊天叫作信件。'
                         'received_at是程序接收时间，sent_at才是平台发送时间；未知时间不猜测。'
                         '必须比较日期和时间，昨晚与今早不是连说两次。历史中的今天、今晚属于当时语境。'
                         '后续回合不等于又过一天。'
                         '截断前的消息未展示，不推断完整交流次数。当前用户消息在本轮输入中，只出现一次。'
                         'user_letter 是用户原话，linli_reply 是林离原话。' + FACT_ATTRIBUTION_BOUNDARY,
              'letters': []}
    if frozen is not None:
        packet['meaning'] = packet['meaning'].replace('按接收顺序排列', '按本轮冻结窗口排列') + (
            '同对话尚未确认回复的输入按平台发送时间插入，发送时间未知时使用接收顺序；'
            'user_received_reply_unconfirmed只有用户原话，不代表林离已经回应。')
    sources = []
    # Continuity gets first use of the existing budget; old retrieval only uses
    # spare capacity, never displacing a just-delivered answer.
    recent_budget = max_chars
    for row in reversed(candidates):
        item = {'source_id': f"reply:{row['letter_id']}:{row.get('reply_revision', 1)}",
                'channel': row.get('channel') or 'letter', 'origin': row.get('origin', 'user'),
                'received_at': _time(row.get('life_received_at')),
                'sent_at': _time(row.get('user_sent_at')),
                'replied_at': _time(row.get('private_world_occurred_at')),
                'user_letter': row.get('content', ''), 'linli_reply': row.get('reply_text', '')}
        if row.get('_received_only'):
            item['delivery_state'] = 'user_received_reply_unconfirmed'
            item['source_message_ids'] = list(row.get('source_messages', {}))
        if row.get('image_delivery_status') == 'DELIVERED':
            item['image_delivery_confirmed'] = True
        deliveries = delivery_references(row)
        from runtime.image_understanding import image_evidence
        images = image_evidence(row)
        if images:
            item['image_observations'] = images
        from runtime.incoming_media import evidence, BOUNDARY as MEDIA_BOUNDARY
        media = evidence(row)
        if media:
            item['incoming_media_observations'] = media
            item['incoming_media_meaning'] = MEDIA_BOUNDARY
        if deliveries:
            item['media_deliveries'] = grouped_delivery_evidence(deliveries)
            item['media_outcome'] = delivery_outcome(row)
        packet['letters'].insert(0, item)
        if len(json.dumps(packet, ensure_ascii=False)) > recent_budget:
            packet['letters'].pop(0)
            if not packet['letters']:
                item['truncated'] = False
                # Preserve both ends so corrections at the end remain visible.
                for key in ('user_letter', 'linli_reply'):
                    value = item[key]
                    if len(value) > 1000:
                        item['truncated'] = True
                        item[key] = value[:500] + '\n[中间原文省略]\n' + value[-500:]
                packet['letters'].append(item)
                # Optional observations must fit the remaining budget, not a
                # fixed quota that can displace the latest correction.
                images = item.get('image_observations', [])
                images = images + item.get('incoming_media_observations', [])
                for edge in (120, 60, 24):
                    for image in images:
                        summary = image['summary']
                        if len(summary) > edge * 2:
                            image['summary'] = summary[:edge] + '\n[中间观察省略]\n' + summary[-edge:]
                            image['summary_truncated'] = True
                    if len(json.dumps(packet, ensure_ascii=False, separators=(',', ':'))) <= recent_budget:
                        break
                while images and len(json.dumps(packet, ensure_ascii=False, separators=(',', ':'))) > recent_budget:
                    removed = images.pop(0)
                    for field in ('image_observations', 'incoming_media_observations'):
                        if removed in item.get(field, []):
                            item[field].remove(removed)
                    item['image_observations_omitted'] = True
                sources.append(item['source_id'])
            break  # Never jump over a missing exchange and call the result continuous.
        sources.append(item['source_id'])
    shown = packet['letters']
    if frozen is not None and len(shown) > WINDOW_STEP and not shown[0].get('truncated'):
        start = candidates[-len(shown)].get('_read_order', len(candidates) - len(shown))
        drop = -start % WINDOW_STEP
        if drop:
            gone = {item['source_id'] for item in shown[:drop]}
            packet['letters'] = shown[drop:]
            sources = [source for source in sources if source not in gone]
    recent = json.dumps(packet, ensure_ascii=False, separators=(',', ':'))
    if len(recent) > max_chars:
        # Do not substitute older messages and call them the continuous tail.
        omitted = json.dumps({'kind': 'recent_dialogue', 'coverage': 'omitted_due_to_capacity',
                              'letters': []}, separators=(',', ':'))
        return (omitted if len(omitted) <= max_chars else ''), ''
    historical = recent_correspondence((r for r in rows if delivered(r)), query=query,
        excluded_sources=(*excluded_sources, *sources), max_chars=max_chars - len(recent))
    if historical:
        history = json.loads(historical)
        history['meaning'] = ('历史检索参考，绝不是刚刚的连续聊天。只在当前话题确实需要时使用；'
                              '旧计划、旧状态不可直接当成现在，用户当前更正优先。' + history['meaning'])
        historical = json.dumps(history, ensure_ascii=False, separators=(',', ':'))
        if len(recent) + len(historical) > max_chars:
            historical = ''
    return recent, historical
