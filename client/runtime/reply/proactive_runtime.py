"""Shared development initiative context; decisions never confer delivery authority."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os

from runtime.personal_chat.initiative_profile import profile_from_snapshot, unanswered_wait
from runtime.personal_chat.initiative import controls_available
from runtime.reply.companion_runtime import CompanionRuntimeError


def enabled():
    return bool(os.environ.get('OLIVIA_JEV_DECISION_URL', '').strip())


def live_profile(server):
    return profile_from_snapshot(server.private_world_port.snapshot())


@contextmanager
def contact_slot(server, channel):
    # Both schedulers run on the same app event loop. Claim before any await.
    acquired = not getattr(server, '_jev_contact_channel', None)
    if acquired:
        server._jev_contact_channel = channel
    try:
        yield acquired
    finally:
        if acquired:
            server._jev_contact_channel = None


def world_gates(snapshot):
    rhythm = snapshot.get('rhythm', {})
    phase = rhythm.get('phase')
    if phase in {'sleep', 'interrupted_rest'}:
        return ['sleeping']
    if phase == 'bathing':
        return ['bathing']
    if phase == 'quiet':
        return ['quiet_hours']
    if snapshot.get('world', {}).get('schedule', {}).get('current_class'):
        return ['class']
    current = snapshot.get('current') if snapshot.get('stale') is False else None
    if current and current.get('activity_kind') in {'class', 'practice', 'creative', 'errand', 'housework'}:
        return ['class' if current['activity_kind'] == 'class' else 'busy']
    if current and current.get('activity_kind') in {'rest', 'reading', 'walk'}:
        return []
    return [] if rhythm.get('availability') == 'open' else ['busy']


def _stamp(row):
    for key in ('delivered_at', 'published_at', 'replied_at', 'created_at'):
        value = row.get(key)
        if type(value) in (int, float):
            return float(value)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
            except ValueError:
                pass
    return 0.0


def _delivered(row):
    return (row.get('delivery_status') == 'DELIVERED' if row.get('channel') in {'qq', 'wechat'}
            else row.get('letter_status') == 'COMPLETED')


def contact_summary(rows, now):
    history = sorted((r for r in rows if _delivered(r) and _stamp(r) <= now.timestamp()), key=_stamp)
    unanswered = 0
    for row in reversed(history):
        if row.get('origin') != 'proactive':
            break
        unanswered += 1
    return {'seconds_since_last_contact': max(0, now.timestamp() - _stamp(history[-1])) if history else None,
            'unanswered_count': unanswered}


def _input_stamp(row):
    for key in ('user_sent_at', 'life_received_at'):
        value = row.get(key)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.utcoffset() is not None:
                    return parsed.timestamp()
            except ValueError:
                pass
    return float(row.get('created_at', 0) or 0)


def contact_gates(rows, *, now, profile, channel, exclude_id=None, appointment_due=False):
    rows = [r for r in rows if exclude_id is None or r.get('letter_id') != exclude_id]
    stamp = now.timestamp()
    ordered = sorted(rows, key=_input_stamp)
    # A newer received pause may still be awaiting extraction: unresolved input
    # blocks separately. Never let a self-authored proactive row clear a pause.
    canonical_users = [r for r in ordered if r.get('origin') != 'proactive' and not r.get('superseded_by')]
    user_rows = [r for r in canonical_users if _delivered(r) or controls_available(r)]
    preference = next((r for r in reversed(user_rows) if r.get('initiative_preference')), {})
    paused = preference.get('initiative_preference') == 'pause' and (
        preference.get('pause_until') is None or stamp < preference['pause_until'])
    if channel == 'letter':
        letter = next((r for r in reversed(user_rows) if r.get('letter_preference')), {})
        paused = paused or (letter.get('letter_preference') == 'pause' and (
            letter.get('letter_until') is None or stamp < letter['letter_until']))
    reasons = []
    latest_user = canonical_users[-1] if canonical_users else None
    if (not appointment_due and latest_user and latest_user.get('companion_timing') == 'wait_user'
            and latest_user.get('silence_reason') == 'USER_REQUESTED_WAIT'):
        reasons.append('user_waiting')
    if any(not r.get('superseded_by') and (r.get('delivery_status') in {
            'RECEIVED', 'GENERATING', 'GENERATED', 'SENDING', 'DELIVERY_UNCONFIRMED'}
            or r.get('delivery_status') == 'FAILED' and r.get('origin') != 'proactive'
            or r.get('letter_status') in {'PENDING', 'PROCESSING'}) for r in rows):
        reasons.append('pending_reply')
    summary = contact_summary(rows, now)
    elapsed = summary['seconds_since_last_contact']
    if elapsed is not None and elapsed < profile.im_interval_min:
        reasons.append('cooldown')
    if summary['unanswered_count'] and elapsed is not None and elapsed < unanswered_wait(
            profile, None, summary['unanswered_count']):
        reasons.append('unanswered_limit')
    return {'paused': bool(paused), 'blocked_reasons': reasons}


def live_state(server, *, channel, now, exclude_id=None, appointment_due=False):
    try:
        profile = live_profile(server)
        snapshot = server.daily_life_runtime.store.snapshot(now)
        rows = deepcopy([r for r in [*server.store.letters, *server.store.personal_chats]
                         if exclude_id is None or r.get('letter_id') != exclude_id])
        gates = contact_gates(rows, now=now, profile=profile, channel=channel, appointment_due=appointment_due)
        # A contact time the user asked for is kept even if she was asleep, in
        # class or busy: people change plans for it. Contact rules still apply.
        world = [] if appointment_due else world_gates(snapshot)
        gates['blocked_reasons'] = list(dict.fromkeys([*world, *gates['blocked_reasons']]))
        return {'profile': profile, 'snapshot': snapshot, 'rows': rows, 'gates': gates}
    except Exception:
        raise CompanionRuntimeError('JEV_CONTEXT_UNAVAILABLE') from None


def state_binding(state):
    """Changes that invalidate a decided contact, excluding clock/weather drift."""
    snapshot = state['snapshot']
    return {'relationship': state['profile'].public_view(),
            'current': snapshot.get('current'), 'stale': snapshot.get('stale'),
            'class': snapshot.get('world', {}).get('schedule', {}).get('current_class')}


def opportunities(state, now):
    from runtime.reply.proactive_letters import make_context
    candidates = make_context(state['rows'], now=now.timestamp(), world=state['snapshot'],
                              profile=state['profile'])['candidates']
    kinds = {'correspondence_followup': 'followup', 'shared_followup': 'shared_topic',
             'relationship_checkin': 'connection', 'affection_checkin': 'connection', 'life_share': 'daily_share'}
    result = []
    for candidate in candidates:
        if candidate['not_before'] <= now.timestamp() < candidate['expires_at'] and candidate['kind'] in kinds:
            # Identity, eligibility and relationship gates remain local. The
            # judgment only needs this opportunity's source and time boundary.
            result.append({'id': candidate['id'], 'kind': kinds[candidate['kind']],
                           'description': json.dumps({k:v for k,v in candidate.items()
                               if k not in {'id','relationship_tier','relationship_caution','window_start'}},
                               ensure_ascii=False, separators=(',', ':'))})
    return result[:16]


def packet(*, channel, now, profile, world, rhythm, emotion, messages, opportunities,
           rows, available_media, hard_gates):
    from runtime.reply.reply_model_quality import _recent_dialogue
    recent = _recent_dialogue(messages)[-4:]
    if any(row.get('truncated') for row in recent):
        raise CompanionRuntimeError('JEV_CONTEXT_UNAVAILABLE')
    world, rhythm, emotion = world or {}, rhythm or {}, emotion or {}
    projected = {k:world[k] for k in ('kind','status','stale','as_of','source_status') if k in world}
    schedule = world.get('schedule') or (world.get('world') or {}).get('schedule') or {}
    projected['schedule'] = {k:schedule[k] for k in ('current_class','next_class') if k in schedule}
    project_ids = set()
    for opportunity in opportunities:
        try:
            detail = json.loads(opportunity['description'])
        except (ValueError, TypeError, KeyError):
            continue
        if isinstance(detail, dict) and isinstance(detail.get('project_id'), str):
            project_ids.add(detail['project_id'])
    for field in ('shared','threads','projects'):
        selected = [{k:v for k,v in item.items() if k in {'id','title','detail','quote','actor','status','updated_at'}}
                    for item in world.get(field, []) if item.get('id') in project_ids]
        if selected:
            projected[field] = selected
    projected['coverage'] = '只含当前状态及本次机会引用事项；近期原话最多两回合，未展示不代表不存在。'
    return deepcopy({'channel': channel, 'as_of': now.isoformat(),
        'relationship': {'tier': profile.tier, 'caution': profile.caution},
        'world': projected, 'rhythm': {k:rhythm[k] for k in ('phase','rest','availability','wellbeing','note') if k in rhythm},
        'activity': {k:v for k,v in (world.get('current') or {}).items()
                     if k in {'activity','activity_kind','location','note','occurred_at'}},
        'emotion': {'status':emotion.get('status'), 'current_affect':
                    {k:v for k,v in (emotion.get('current_affect') or {}).items()
                     if k in {'label','intensity','reason','status','as_of','pending_sources'}}},
        'recent_dialogue': [{'role': r['role'], 'content': r['text']} for r in recent],
        'opportunities': opportunities, 'contact': contact_summary(rows, now),
        'available_media': list(available_media), 'hard_gates': hard_gates})


async def decide(value, port=None):
    from runtime.reply.companion_proactive import configured_proactive
    port = port or configured_proactive()
    if port is None:
        raise CompanionRuntimeError('JEV_CONTEXT_UNAVAILABLE')
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    result = await port.evaluate(json.loads(encoded))
    if result.decision is None:
        raise CompanionRuntimeError(result.error_code or 'JEV_UNAVAILABLE')
    if result.input_digest != hashlib.sha256(encoded.encode()).hexdigest():
        raise CompanionRuntimeError('JEV_RESPONSE_INVALID')
    return result


def record_decision(result):
    return {'decision': result.decision, 'input_digest': result.input_digest,
            'response_digest': result.response_digest, 'schema_digest': result.schema_digest,
            'as_of': json.loads(result.input_json)['as_of']}


def project_decision(messages, result, *, max_input_chars):
    value = json.loads(result.input_json)
    payload = {'decision': result.decision, 'relationship': value['relationship'],
               'rhythm': value['rhythm'], 'opportunities': value['opportunities']}
    note = ('这是应用主动联系机会，不是用户新发言，也不是重新回答上一句话。'
            '主动判断已完成，只围绕选定意图写正文，使用指定medium；不要再次决定skip或切换载体。'
            '关系档位只是联系积极度，不证明恋爱身份。亲密时可以黏人、分享小事、想说两句，'
            '不催回复，不编造刚发生的共同经历，不要求解释沉默。不得把内部判断、档位或来源写给用户。'
            '下方rhythm为本次判断同一时刻的节律，计划不是已发生事实；语音无情绪指令与速度控制。\n'
            '<proactive_decision>' + json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('<', r'\u003c')
            .replace('>', r'\u003e') + '</proactive_decision>')
    result_messages = [*messages, {'role': 'system', 'content': note}]
    if sum(len(m.get('content', '')) for m in result_messages) > max_input_chars:
        raise CompanionRuntimeError('JEV_CONTEXT_BUDGET_EXCEEDED')
    return tuple(result_messages)
