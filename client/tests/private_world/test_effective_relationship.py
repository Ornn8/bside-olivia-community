from copy import deepcopy
from types import SimpleNamespace

import pytest

from runtime.memory.effective_relationship import effective_relationship_stage


def snapshot(stage='familiar', level='high', tension='low'):
    return SimpleNamespace(relationship_stage=stage, familiarity=level, trust=level,
                           comfort=level, closeness=level, tension=tension)


@pytest.mark.parametrize('days14,days21,expected', [(4,7,'familiar'), (5,7,'friend'), (5,8,'trusted_friend')])
def test_day_boundaries(days14, days21, expected):
    state = snapshot()
    before = deepcopy(state)
    assert effective_relationship_stage(state, {14: days14, 21: days21}) == expected
    assert state == before


@pytest.mark.parametrize('name', ['familiarity', 'comfort'])
def test_each_everyday_level_boundary(name):
    state = snapshot()
    setattr(state, name, 'medium')
    assert effective_relationship_stage(state, {14: 5, 21: 8}) == 'friend'
    setattr(state, name, 'low')
    assert effective_relationship_stage(state, {14: 5, 21: 8}) == 'familiar'


@pytest.mark.parametrize('name', ['trust', 'closeness'])
def test_each_deep_level_boundary(name):
    state = snapshot()
    setattr(state, name, 'medium')
    assert effective_relationship_stage(state, {14: 5, 21: 8}) == 'trusted_friend'
    setattr(state, name, 'low')
    assert effective_relationship_stage(state, {14: 5, 21: 8}) == 'friend'
    setattr(state, name, 0)
    assert effective_relationship_stage(state, {14: 5, 21: 8}) == 'familiar'


def test_month_of_daily_letters_is_a_friend_not_unknown():
    # Real shape after ~30 active days: everyday levels high, rare evidence low, no tension.
    state = SimpleNamespace(relationship_stage='unknown', familiarity=100, trust=13,
                            comfort=74, closeness=8, tension=0)
    assert effective_relationship_stage(state, {14: 14, 21: 21}) == 'friend'
    assert effective_relationship_stage(state, {14: 2, 21: 2}) == 'familiar'


@pytest.mark.parametrize('familiarity,expected', [(0, 'unknown'), (10, 'acquaintance'), (40, 'familiar')])
def test_unconfirmed_stage_reflects_familiarity(familiarity, expected):
    state = SimpleNamespace(relationship_stage='unknown', familiarity=familiarity, trust=0,
                            comfort=0, closeness=0, tension=0)
    assert effective_relationship_stage(state, {14: 0, 21: 0}) == expected


@pytest.mark.parametrize('stage', ['unknown', 'acquaintance', 'familiar', 'close', 'committed'])
def test_romantic_stages_are_only_preserved(stage):
    result = effective_relationship_stage(snapshot(stage), {14: 14, 21: 21})
    assert result == (stage if stage in ('close', 'committed') else 'trusted_friend')


@pytest.mark.parametrize('tension,expected', [(0,'trusted_friend'), ('low','trusted_friend'), ('medium','friend'), ('high','familiar')])
def test_tension(tension, expected):
    assert effective_relationship_stage(snapshot(tension=tension), {14: 5, 21: 8}) == expected


@pytest.mark.parametrize('score,expected', [(34,'familiar'), (35,'friend'), (69,'friend'), (70,'trusted_friend')])
def test_numeric_level_boundaries(score, expected):
    state = snapshot(level=score, tension=1)
    assert effective_relationship_stage(state, {14:5,21:8}) == expected


def test_projection_and_writer_keep_permissions_and_ledger_unchanged(tmp_path):
    from dataclasses import replace
    from private_world_ledger import SQLitePrivateWorldLedger
    from runtime.memory.private_world_projection import project_private_world
    from runtime.persona.persona_assembly import _private_behavior_payload
    ledger = SQLitePrivateWorldLedger(tmp_path / 'world.sqlite3')
    before = ledger.snapshot()
    state = replace(before, relationship_stage='familiar', familiarity=70, trust=70, comfort=70, closeness=70, tension=1)
    confirmed = project_private_world(state)
    derived = project_private_world(state, {14:5,21:8})
    assert derived.behavior.relationship_stage.value == 'trusted_friend'
    assert derived.intimacy_ceiling == confirmed.intimacy_ceiling
    assert derived.granted_intimacy == confirmed.granted_intimacy
    assert derived.authorized_nicknames == confirmed.authorized_nicknames
    assert derived.may_acknowledge_home_history == confirmed.may_acknowledge_home_history
    view = _private_behavior_payload(SimpleNamespace(private_behavior=derived.behavior, world_state_available=True))
    assert view['relationship_stage'] == 'trusted_friend'
    assert '不是确认过的恋爱关系' in view['relationship_stage_meaning']
    assert ledger.snapshot() == before and state.relationship_stage == 'familiar'


def test_real_reply_context_and_panel_use_same_history(monkeypatch):
    import time
    import local_server
    from original_client_server import PrivateWorldControlReadAdapter
    from private_world_port import PrivateWorldSnapshot, NullPrivateWorldPort
    from runtime.memory.effective_relationship import relationship_history
    from runtime.reply.reply_context import ReplyMode
    now = time.time()
    state = PrivateWorldSnapshot(relationship_stage='familiar', familiarity=70, trust=70, comfort=70, closeness=70, tension=1)
    class Port(NullPrivateWorldPort):
        def snapshot(self): return state
    port = Port()
    rows = [dict(letter_id=str(i), content='synthetic', created_at=now-i*86400-1) for i in range(8)]
    store = SimpleNamespace(letters=rows, personal_chats=[])
    monkeypatch.setattr(local_server, 'store', store)
    monkeypatch.setattr(local_server.letters_adapter, 'private_world_port', port)
    context = local_server.letters_adapter.build_reply_context(ReplyMode.TEXT_LETTER)
    panel = PrivateWorldControlReadAdapter(port, lambda: relationship_history(store)).snapshot()
    assert context.private_behavior.relationship_stage.value == panel['relationship_stage'] == 'trusted_friend'


def test_history_reuses_receipts_deduplicates_days_and_excludes_proactive():
    from runtime.memory.effective_relationship import relationship_history
    from tests.http.test_proactive_engagement import NOW, messages
    rows = messages(80, 8)
    rows += [dict(rows[-1], origin='proactive', created_at=NOW+1)]
    store = SimpleNamespace(letters=[], personal_chats=rows)
    assert relationship_history(store, NOW) == {14:8,21:8}


def test_friendship_drives_cadence_without_romantic_tone():
    from runtime.personal_chat.initiative_profile import profile_from_snapshot
    from runtime.personal_chat.engagement import adapt_profile
    state = snapshot(level=70, tension=1)
    profile = profile_from_snapshot(state, {14:5,21:8})
    assert profile.tier == 'trusted'
    result = adapt_profile(profile, 4, True)
    assert result.tier == 'committed' and result.tone_tier == 'trusted'
