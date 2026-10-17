"""Qualitative relationship page and short, provider-free writer context."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import uuid

from runtime.cloud_policy import get_policy
from runtime.relationship_growth import route, volume_updates
from runtime.relationship_policy import ROUTES, volume_catalog
from runtime.relationship_moments import moments, plans


def ledger(server):
    committer = server.private_world_relationship_committer
    if committer is None:
        raise RuntimeError('RELATIONSHIP_UNAVAILABLE')
    return committer.ledger


def route_changes(state, chosen):
    if not isinstance(chosen, str) or chosen not in ROUTES:
        raise ValueError('RELATIONSHIP_ROUTE_INVALID')
    changes = volume_updates(state, {'relationship_route': chosen}, len(state.relationship_days))
    # Identity comes from the explicit page choice; progression still comes from interaction.
    if chosen != 'romantic' and state.relationship_stage in {'close', 'committed'}:
        changes['relationship_stage'] = 'trusted_friend'
    return changes


def change_route(target, chosen, version, now=None):
    from runtime.private_world.ledger import LedgerEvent
    state = target.snapshot()
    if type(version) is not int or version != state.version:
        raise ValueError('RELATIONSHIP_VERSION_CONFLICT')
    changes = route_changes(state, chosen)
    identity = 'route.' + uuid.uuid4().hex
    target.apply_once(LedgerEvent(identity, identity, 'relationship_route', {'route': chosen},
                                 (now or datetime.now(timezone.utc)).isoformat()),
                      replace(state, version=state.version + 1, **changes), expected_snapshot_version=version)


def intimacy_line(state, *, minor=False):
    if minor:
        return '未成年人不进入亲密互动，以普通陪伴自然回应。'
    if route(state) in {'friend', 'family'}:
        return '保持友情或家人式陪伴，不使用恋爱与亲密动作。'
    ladder = {1: '先用语言表达好感', 2: '可以文字拥抱', 3: '可以牵手、拥抱、亲脸',
              4: '可以亲吻', 5: '可以更亲密的温柔互动，绝不露骨'}
    return ('亲密分寸：' + ladder[min(5, state.relationship_volume)] +
            '。按对方这次的意愿自然回应，不把亲密当任务；不合适时温柔转开，不讲规则。未成年人一律不进入亲密。')


def writer_line(state, target=None, life_store=None):
    state = replace(state, **volume_updates(state, {}, len(state.relationship_days)))
    texts = get_policy('relationship_texts')
    line = texts['route_lines'][route(state)] + '\n' + intimacy_line(state)
    line += '\n用户提到共同经历时自然接住；记不清就说“记不清，你再说说”，不否认，也不编造自己记得的细节。'
    if target is not None:
        recent = moments(target)
        remembered = [texts['moments'][r['kind']] + ('：' + r['user_quote'] if not r['sensitive'] else '') for r in recent]
        if remembered:
            line += '\n共同瞬间（资料）：' + '；'.join(remembered)
    pending = plans(life_store)[:5]
    if pending:
        line += '\n未完成约定（资料）：' + '；'.join(str(p.get('title', ''))[:60] for p in pending)
    if route(state) == 'undecided' and state.relationship_volume == 4 and not state.route_asked:
        line += '\n' + texts['route_ask']
    return line


def acknowledge_route_ask(target, now):
    """Only consume the one-time invitation after a canonical reply is delivered."""
    from runtime.private_world.ledger import LedgerEvent
    state = target.snapshot()
    changes = volume_updates(state, {}, len(state.relationship_days))
    if route(state) != 'undecided' or changes['relationship_volume'] != 4 or state.route_asked:
        return
    identity = 'route-ask.' + uuid.uuid4().hex
    target.apply_once(LedgerEvent(identity, identity, 'relationship_route_ask', {}, now.isoformat()),
        replace(state, version=state.version + 1, route_asked=True, **changes), expected_snapshot_version=state.version)


def details(server, now=None):
    from runtime.memory.effective_relationship import level
    now = now or datetime.now(timezone.utc)
    target = ledger(server)
    state = target.snapshot()
    # Project legacy records without writing on GET.
    state = replace(state, **volume_updates(state, {}, len(state.relationship_days)))
    texts = get_policy('relationship_texts')
    recent = moments(target)
    week = (now - timedelta(days=7)).date().isoformat()
    rising = {r[2] for r in state.relationship_growth if r[0] >= week and r[3] > 0}
    life = getattr(server.daily_life_runtime, 'store', None)
    addressing = {} if life is None else {key: rows[0]['quote'][:16]
        for key, rows in life.addressing_profile(now=now, per_kind=1).items() if rows}
    dynamic = next((texts['rising'][k] for k in ('trust', 'closeness', 'comfort', 'familiarity') if k in rising),
                   '你们的关系，藏在每一次认真回应里。')
    if state.tension >= 35:
        dynamic = '有些小别扭还没说开，可以慢慢聊。'
    pending = plans(life, now=now)
    hint = ('聊聊那个还没完成的约定吧。' if pending else
            '分享一件今天真正触动你的小事，不必刻意找话题。')
    return {'status': 'READY', 'version': state.version, 'route': route(state), 'routes': ROUTES,
        'volume': state.relationship_volume, **volume_catalog(state, now), 'dynamic': dynamic,
        'dimensions': {key: {'text': texts['dimensions'][key][level(state, key)], 'rising': key in rising}
                       for key in ('familiarity', 'trust', 'comfort', 'closeness')},
        'moments': [{'date': r['occurred_at'][:10], 'text': texts['moments'][r['kind']],
                     'quote': '' if r['sensitive'] else r['user_quote'][:40]} for r in recent],
        'plans': [{'title': p.get('title', ''), 'detail': p.get('detail', '')} for p in pending[:5]],
        'addressing': addressing, 'hint': hint}
