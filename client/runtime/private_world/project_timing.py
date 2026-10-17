"""Source-versioned interpretation of project time scope, never proof of completion."""
from datetime import datetime, timedelta, timezone
import hashlib
import json

LOCAL = timezone(timedelta(hours=8))
CONTRACT_REVISION = 4


def source_version(project):
    fields = {key: project.get(key) for key in ('id', 'source_id', 'updated_at', 'title', 'detail', 'quote', 'kind', 'status')}
    if fields['quote']:
        fields['detail'] = fields['quote']
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def version(project):
    return hashlib.sha256(f'{CONTRACT_REVISION}:{source_version(project)}'.encode()).hexdigest()


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS life_project_timing (version TEXT PRIMARY KEY, payload TEXT NOT NULL)')


def project_at(db, project, now):
    row = db.execute('SELECT payload FROM life_project_timing WHERE version=?', (version(project),)).fetchone()
    if not row:
        prior = db.execute("SELECT payload FROM life_project_timing WHERE json_extract(payload,'$.id')=? "
            "AND json_extract(payload,'$.source_at')<=? ORDER BY json_extract(payload,'$.source_at') DESC LIMIT 1",
            (project['id'], project.get('updated_at', ''))).fetchone()
        if not prior:
            active = project.get('status') not in {'completed', 'cancelled'}
            # Unknown timing must not create an indefinite obligation. Preserve
            # the source/status: expiry is not evidence that it was fulfilled.
            stamp = datetime.fromisoformat(project['updated_at'])
            deadline = stamp + timedelta(days=7)
            if not active or now < deadline:
                return {**project, 'time_scope_pending': active}
            return {**project, 'time_scope_pending': active,
                    'expired_by_age': True, 'deadline_expired': True}
        timing = json.loads(prior[0])
        # V4 distinguishes old unbounded activities from durable commitments.
        # Already resolved absolute deadlines remain authoritative.
        if (timing['scope'] in {'bounded', 'transient'} or timing['scope'] == 'open' and timing.get('contract_revision', 0) >= 4) and (
                timing.get('source_version', timing['version']) == source_version(project)):
            deadline = timing['deadline_at']
            return {**project, 'time_scope': timing['scope'], 'deadline_at': deadline,
                    'deadline_expired': bool(deadline and now >= datetime.fromisoformat(deadline)
                        and project['status'] not in {'completed', 'cancelled'})}
        deadline = timing['deadline_at']
        return {**project, 'previous_time_scope': {key: timing[key] for key in ('scope', 'deadline_at', 'source_at')}, 'time_scope_pending': True,
                'deadline_at': deadline, 'deadline_expired': bool(deadline
                    and now >= datetime.fromisoformat(deadline)
                    and project['status'] not in {'completed', 'cancelled'})}
    timing = json.loads(row[0])
    deadline = timing['deadline_at']
    return {**project, 'time_scope': timing['scope'], 'deadline_at': deadline,
            'deadline_expired': bool(deadline and now >= datetime.fromisoformat(deadline)
                                     and project['status'] not in {'completed', 'cancelled'})}


def pending(projects):
    return [(index, project) for index, project in enumerate(projects)
            if project.get('status') not in {'completed', 'cancelled'}
            and 'time_scope' not in project and project.get('updated_at')]


def inherit_world_progress(db, old, new):
    """A world progress event cannot silently reschedule an existing promise."""
    if old is None:
        return
    row = db.execute('SELECT payload FROM life_project_timing WHERE version=?', (version(old),)).fetchone()
    if not row:
        row = db.execute('SELECT payload FROM life_project_timing WHERE version=?', (source_version(old),)).fetchone()
    if not row:
        row = db.execute("SELECT payload FROM life_project_timing WHERE json_extract(payload,'$.source_version')=? "
            "AND json_extract(payload,'$.scope') IN ('open','bounded') ORDER BY rowid DESC LIMIT 1",
            (source_version(old),)).fetchone()
    if row:
        previous = json.loads(row[0])
        if previous['scope'] == 'open' and previous.get('contract_revision', 0) < 4:
            return
        timing = {**previous, 'version': version(new), 'source_at': new['updated_at'],
                  'source_version': source_version(new), 'contract_revision': CONTRACT_REVISION}
        db.execute('INSERT OR IGNORE INTO life_project_timing VALUES (?,?)',
                   (timing['version'], json.dumps(timing, ensure_ascii=False)))


def prepare_context(data):
    contexts = {}
    for index, project in pending(data['projects']):
        stamp = datetime.fromisoformat(project['updated_at']).astimezone(LOCAL)
        context = {'source_local_time': stamp.isoformat(), 'target_quote': project.get('quote', ''),
                   'actor': project.get('actor', 'linli')}
        original = data.get('project_source_contexts', {}).get(project.get('source_id'))
        if original and original.get('coverage') == 'complete':
            context.update(reply_text=original['segments'][0]['text'], coverage='complete')
        elif original:
            context.update(reply_segments=original['segments'], coverage='partial')
        else:
            context.update(reply_text=project.get('quote', ''), coverage='quote_only')
        if project.get('previous_time_scope'):
            context['previous_time_scope'] = project['previous_time_scope']
        contexts[f'p{index}'] = context
    return {**data, 'project_time_contexts': contexts}


def attach_source_context(data, rows):
    """Join exact delivered sources, never a recent unrelated dialogue tail."""
    wanted = {project.get('source_id'): project for _, project in pending(data['projects'])}
    contexts = {}
    now = datetime.fromisoformat(data['time'])
    for row in rows:
        if not isinstance(row, dict) or row.get('read_only') or not row.get('letter_id'):
            continue
        source = f"reply:{row['letter_id']}:{row.get('reply_revision', 1)}"
        if source not in wanted or (row.get('channel') or 'letter') not in {'letter', 'qq', 'wechat'}:
            continue
        chat = row.get('channel') in {'qq', 'wechat'}
        if (row.get('delivery_status') != 'DELIVERED' if chat else
                row.get('letter_status') != 'COMPLETED' or row.get('private_world_status') != 'COMMITTED'):
            continue
        try:
            stamp = datetime.fromisoformat(row['private_world_occurred_at'].replace('Z', '+00:00'))
            if (stamp > now or stamp != datetime.fromisoformat(wanted[source]['updated_at'])
                    or float(row.get('reply_not_before') or 0) > now.timestamp()):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        text, quote = row.get('reply_text'), wanted[source].get('quote')
        if not isinstance(text, str) or not isinstance(quote, str) or not quote or quote not in text:
            continue
        digest = row.get('private_world_reply_sha256')
        if digest and digest != hashlib.sha256(text.encode()).hexdigest():
            continue
        if len(text) <= 1000:
            spans = [(0, len(text))]
        else:
            pos = text.index(quote)
            width = min(240, max(160, len(quote)))
            start = max(0, pos - min(40, max(0, width - len(quote))))
            end = min(len(text), start + width)
            spans = [(start, end)]
            tail = 240 - (end - start)
            if tail and end < len(text):
                tail_start = max(end, len(text) - tail)
                spans.append((tail_start, len(text)))
        contexts[source] = {'actor': 'character', 'occurred_at': stamp.isoformat(),
            'coverage': 'complete' if spans == [(0, len(text))] else 'partial',
            'source_length': len(text), 'segments': [{'start': start, 'end': end, 'text': text[start:end]}
                                                  for start, end in spans]}
    return {**data, 'project_source_contexts': contexts}


def questions(data):
    result = {}
    for index, _ in pending(data['projects']):
        name = f'project_time_{index}'
        for field, criteria in (
            ('scope', {'open': '持续项目/长期目标/无截止期承诺', 'transient': '仅本次当下活动或状态观察，不是持续待办', 'bounded': '有可确定截止上限，含几点之前/不到', 'unclear': '原文时间确实无法消歧'}),
            ('day', {'unknown': '不确定', **{str(i): str(i) for i in range(32)}}),
            ('hour', {'unknown': '不确定', **{str(i): str(i) for i in range(24)}}),
            ('minute', {'unknown': '不确定', **{str(i): str(i) for i in range(60)}})):
            if (field == 'scope' and data['projects'][index].get('previous_time_scope', {}).get('scope') == 'bounded'):
                criteria = {**criteria, 'inherit': '本轮未明确改期，保持已有绝对时限/长期解释'}
            meaning = {'scope': '原话有效时限类型', 'day': '截止日距原source北京时间日期的天数（凌晨睡前说下一觉早起取0）',
                       'hour': '截止北京时间小时（不是source小时）', 'minute': '截止分钟：七点前为00，不能抄source分钟'}[field]
            result[name + '_' + field] = {'instructions': f'读局部p{index}原回复：{meaning}；按project_time_contract独立判断。', 'criteria': criteria}
    return result


CONTRACT = (
    'pN=context.project_time_contexts.pN；target_quote定位事项，reply_text是精确source/revision已发送原文；'
    'reply_segments的start/end为真实偏移，coverage=partial是非连续节选，不能当全文。仅用本封原文+source_local_time，不用人格/课表/其它事件，不新增事实或改status。'
    'open=持续目标/长期练习研究/无期限承诺；transient=仅当时活动状态、无持续目标约定，不因无截止就open，不代表已完成/取消。'
    'bounded=可确定截止上限；unclear=原文无法消歧/表达。旧unclear须重判；旧open须区分open/transient，不能inherit；'
    'previous_time_scope为既有绝对解释，bounded无明确改期/撤回则inherit，普通进度不能把旧“明天”推后。'
    '相对日期只锚source北京时间，不锚查询日；凌晨睡前告别+明天早起优先下一觉同日上午，除非明指下一自然日；仅明天无消歧才unclear。'
    '各题独立读本封，不等其它答案；可确定就选数字。day=source自然日偏移0..31，hour/minute=截止上限本地钟点；'
    '仅某日取23:59；某整点之前/不到取整点，如七点不到=7:00，不猜实际分钟或unknown。open/transient/unclear/inherit钟点不适用。'
    '不因时间经过判完成，不从用户假设或缺课表造deadline。')


def compile_answers(data, answers):
    values = []
    for index, project in pending(data['projects']):
        prefix = f'project_time_{index}_'
        if prefix + 'scope' not in answers:
            continue  # Explicit budget-deferred source version; never fabricate a result.
        scope, deadline = answers[prefix + 'scope'], None
        raw_scope = scope
        fields = [answers[prefix + field] for field in ('day', 'hour', 'minute')]
        missing = [field for field, value in zip(('day', 'hour', 'minute'), fields) if value == 'unknown']
        stage = 'scope_unclear' if scope == 'unclear' else scope if scope in {'open', 'transient'} else 'resolved'
        if scope == 'inherit':
            previous = project['previous_time_scope']
            scope, deadline = previous['scope'], previous['deadline_at']
            stage = 'inherited'
        elif scope == 'bounded':
            if 'unknown' in fields:
                scope = 'unclear'
                stage = 'deadline_fields_unknown'
            else:
                day, hour, minute = map(int, fields)
                stamp = datetime.fromisoformat(project['updated_at']).astimezone(LOCAL)
                deadline = (stamp.replace(hour=hour, minute=minute, second=0, microsecond=0)
                            + timedelta(days=day)).astimezone(timezone.utc).isoformat()
        values.append({'id': project['id'], 'version': version(project), 'scope': scope, 'deadline_at': deadline,
                       'source_at': project['updated_at'], 'source_version': source_version(project),
                       'contract_revision': CONTRACT_REVISION,
                       'diagnostic': {'project_index': index, 'scope': raw_scope,
                           **dict(zip(('day', 'hour', 'minute'), fields)),
                           'minute_tens': answers.get(prefix + 'minute_tens'), 'minute_ones': answers.get(prefix + 'minute_ones'),
                           'missing_fields': missing if raw_scope == 'bounded' else [], 'stage': stage}})
    return values


def validate(values, data):
    available = {(p['id'], version(p)): p for p in data['projects']}
    seen = set()
    if not isinstance(values, list) or len(values) > len(available):
        raise ValueError('DAILY_LIFE_PROJECT_TIMING_INVALID')
    for value in values:
        if not isinstance(value, dict) or set(value) != {'id', 'version', 'scope', 'deadline_at', 'source_at', 'source_version', 'contract_revision', 'diagnostic'}:
            raise ValueError('DAILY_LIFE_PROJECT_TIMING_INVALID')
        identity = value['id'], value['version']
        if (identity not in available or identity in seen or value['scope'] not in {'open', 'bounded', 'unclear', 'transient'}
                or value['source_at'] != available[identity]['updated_at']
                or value['source_version'] != source_version(available[identity])
                or value['contract_revision'] != CONTRACT_REVISION):
            raise ValueError('DAILY_LIFE_PROJECT_TIMING_INVALID')
        seen.add(identity)
        diagnostic = value['diagnostic']
        if (not isinstance(diagnostic, dict) or set(diagnostic) != {'project_index', 'scope', 'day', 'hour', 'minute',
                'minute_tens', 'minute_ones', 'missing_fields', 'stage'}
                or type(diagnostic['project_index']) is not int or diagnostic['project_index'] < 0
                or diagnostic['scope'] not in {'open', 'bounded', 'unclear', 'transient', 'inherit'}
                or diagnostic['stage'] not in {'open', 'transient', 'resolved', 'scope_unclear', 'inherited', 'deadline_fields_unknown'}
                or not isinstance(diagnostic['missing_fields'], list)
                or any(field not in {'day', 'hour', 'minute'} for field in diagnostic['missing_fields'])):
            raise ValueError('DAILY_LIFE_PROJECT_TIMING_INVALID')
        for field, count in (('day', 32), ('hour', 24), ('minute', 60), ('minute_tens', 6), ('minute_ones', 10)):
            choices = {'unknown', *(str(i) for i in range(count))}
            if field.startswith('minute_'):
                choices.add(None)
            if not isinstance(diagnostic[field], (str, type(None))) or diagnostic[field] not in choices:
                raise ValueError('DAILY_LIFE_PROJECT_TIMING_INVALID')
        if value['scope'] != 'bounded':
            if value['deadline_at'] is not None:
                raise ValueError('DAILY_LIFE_PROJECT_TIMING_INVALID')
            continue
        try:
            deadline = datetime.fromisoformat(value['deadline_at'])
            start = datetime.fromisoformat(available[identity]['updated_at']).astimezone(LOCAL).replace(hour=0, minute=0, second=0, microsecond=0)
            inherited = available[identity].get('previous_time_scope', {}).get('deadline_at') == value['deadline_at']
            if deadline.tzinfo is None or not (inherited or start <= deadline < start + timedelta(days=32)):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('DAILY_LIFE_PROJECT_TIMING_INVALID') from None
    return values
