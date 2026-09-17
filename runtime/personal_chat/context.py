"""Keep a chronological IM tail separate from retrieved past correspondence."""
import json
from datetime import datetime, timezone, timedelta

from runtime.reply.recent_correspondence import recent_correspondence
from runtime.reply.initiative_policy import interaction_rhythm

LOCAL = timezone(timedelta(hours=8))


def _time(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.astimezone(LOCAL).isoformat() if stamp.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def chat_context(rows, *, query, now, excluded_sources=(), max_chars=6000):
    rows = list(rows)
    candidates = [r for r in rows if r.get('delivery_status') == 'DELIVERED'
                  and r.get('channel') in {'qq', 'wechat'}
                  and not any(s.startswith(f"reply:{r.get('letter_id')}:") for s in excluded_sources)]
    # Receive order is authoritative even if delivery or indexing completes later.
    candidates.sort(key=lambda r: (float(r.get('created_at', 0)), r['letter_id']))
    packet = {'current_time': now.astimezone(LOCAL).isoformat(), 'timezone': 'Asia/Shanghai',
              'meaning': '以下是按接收顺序排列的最近连续聊天，跨渠道仍是同一个人。'
                         'received_at是程序接收时间，sent_at才是平台发送时间；未知时间不猜测。'
                         '必须比较日期和时间，昨晚与今早不是连说两次。历史中的今天、今晚属于当时语境。'
                         '截断前的消息未展示，不推断完整交流次数。当前用户消息在本轮输入中，只出现一次。',
              'interaction_rhythm': interaction_rhythm(candidates, now.timestamp()),
              'letters': []}
    sources = []
    recent_budget = max_chars * 2 // 3
    for row in reversed(candidates):
        item = {'source_id': f"reply:{row['letter_id']}:{row.get('reply_revision', 1)}",
                'channel': row['channel'], 'origin': row.get('origin', 'user'),
                'received_at': _time(row.get('life_received_at')),
                'sent_at': _time(row.get('user_sent_at')),
                'replied_at': _time(row.get('private_world_occurred_at')),
                'user_letter': row.get('content', ''), 'linli_reply': row.get('reply_text', '')}
        packet['letters'].insert(0, item)
        if len(json.dumps(packet, ensure_ascii=False)) > recent_budget:
            packet['letters'].pop(0)
            if not packet['letters']:
                item['truncated'] = True
                # Preserve both ends so corrections at the end remain visible.
                for key in ('user_letter', 'linli_reply'):
                    value = item[key]
                    if len(value) > 1000:
                        item[key] = value[:500] + '\n[中间原文省略]\n' + value[-500:]
                packet['letters'].append(item)
                sources.append(item['source_id'])
            break  # Never jump over a missing exchange and call the result continuous.
        sources.append(item['source_id'])
    recent = json.dumps(packet, ensure_ascii=False, separators=(',', ':'))
    historical = recent_correspondence(rows, query=query,
        excluded_sources=(*excluded_sources, *sources), max_chars=max_chars - len(recent))
    if historical:
        history = json.loads(historical)
        history['meaning'] = ('历史检索参考，绝不是刚刚的连续聊天。只在当前话题确实需要时使用；'
                              '旧计划、旧状态不可直接当成现在，用户当前更正优先。' + history['meaning'])
        historical = json.dumps(history, ensure_ascii=False, separators=(',', ':'))
        if len(recent) + len(historical) > max_chars:
            historical = ''
    return recent, historical
