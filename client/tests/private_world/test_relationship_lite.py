import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from runtime.private_world.port import PrivateWorldSnapshot

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


def test_route_confirmation_does_not_skip_volumes_or_enable_minors():
    from runtime.relationship_lite import route_changes, intimacy_line
    state = PrivateWorldSnapshot()
    changes = route_changes(state, 'romantic')
    assert changes['relationship_route'] == 'romantic'
    assert changes['relationship_volume'] == 1
    chosen = replace(state, **changes)
    assert '亲吻' not in intimacy_line(chosen)
    assert '不进入亲密' in intimacy_line(replace(chosen, relationship_volume=5), minor=True)
    assert '亲吻' in intimacy_line(replace(chosen, relationship_volume=4))
    assert '不使用恋爱' in intimacy_line(replace(chosen, relationship_route='family'))


def test_growth_everyday_cannot_starve_deep():
    from runtime.relationship_growth import growth_updates, DEFAULTS
    state = PrivateWorldSnapshot()
    for _ in range(50):
        state = replace(state, **growth_updates(state, {'rel_disclosure': 'everyday'}, NOW, DEFAULTS))
    assert state.familiarity == 3
    state = replace(state, **growth_updates(state, {'rel_disclosure': 'vulnerable'}, NOW, DEFAULTS))
    assert state.trust == 2 and state.closeness == 1


def test_queue_due_recovery_and_deleted_messages(tmp_path):
    from runtime.relationship_batch import BatchQueue
    queue = BatchQueue(tmp_path / 'queue.sqlite3')
    rows = [{'letter_id': str(i), 'private_world_delivery_id': 'delivery.' + str(i),
             'content': '今天练完了一首曲子', 'reply_text': '是哪一首？',
             'private_world_occurred_at': NOW.isoformat()} for i in range(50)]
    queue.sync(rows[:49], NOW)
    assert not queue.due(NOW)
    assert queue.due(NOW + timedelta(days=1))
    queue.sync(rows, NOW)
    batch = queue.pending(NOW)
    assert len(batch['turns']) == 50
    assert BatchQueue(queue.path).pending(NOW)['request_id'] == batch['request_id']
    queue.sync(rows[:10], NOW)
    assert len(queue.pending(NOW)['turns']) == 10


def test_batch_rejects_foreign_ids_and_invented_quotes():
    from runtime.relationship_batch import validate_result
    turns = [{'id': 'a', 'user': '我们明天听那首歌', 'reply': '好呀，明天一起听'}]
    result = {'turns': [{'id': 'b', 'signals': {}, 'moment': None}]}
    with pytest.raises(ValueError):
        validate_result(result, turns)
    result['turns'][0] = {'id': 'a', 'signals': {'rel_shared': 'plan'},
                          'moment': 'rel_shared:plan', 'user_quote': '今晚见', 'reply_quote': '好呀'}
    with pytest.raises(ValueError):
        validate_result(result, turns)


def test_batch_recovery_preserves_conversation_chronology(tmp_path):
    from runtime.relationship_batch import BatchQueue
    queue = BatchQueue(tmp_path / 'queue.sqlite3')
    rows = [{'letter_id': identity, 'private_world_delivery_id': identity,
             'content': '我们明天听歌', 'reply_text': '好呀',
             'private_world_occurred_at': (NOW + timedelta(minutes=minute)).isoformat()}
            for identity, minute in [('a-later', 2), ('z-earlier', 1)]]
    queue.sync(rows, NOW + timedelta(days=1))
    assert [turn['id'] for turn in queue.pending(NOW)['turns']] == ['z-earlier', 'a-later']


def test_ladder_and_identity_reach_writer_and_reviewer():
    from runtime.memory.private_world_projection import project_private_world
    from runtime.reply.reply_context import IntimacyTier, RelationshipStage
    state = PrivateWorldSnapshot(relationship_route='romantic', relationship_volume=2)
    view = project_private_world(state)
    assert view.behavior.relationship_stage is RelationshipStage.COMMITTED
    assert view.granted_intimacy is IntimacyTier.LIGHT_CONTACT
    assert project_private_world(replace(state, relationship_route='family')).granted_intimacy is IntimacyTier.NONE
    assert project_private_world(replace(state, relationship_volume=4)).granted_intimacy is IntimacyTier.CLOSE_CONTACT


def test_route_ask_once_after_delivery(tmp_path):
    from runtime.private_world.ledger import SQLitePrivateWorldLedger, LedgerEvent
    from runtime.relationship_lite import writer_line, acknowledge_route_ask
    target = SQLitePrivateWorldLedger(tmp_path / 'world.sqlite3')
    state = PrivateWorldSnapshot(version=2, relationship_route='undecided', relationship_volume=4)
    target.apply_once(LedgerEvent('seed','seed','seed',{},NOW.isoformat()),state)
    assert '这次聊天自然地问一次' in writer_line(state,target)
    assert not target.snapshot().route_asked
    acknowledge_route_ask(target,NOW)
    assert '这次聊天自然地问一次' not in writer_line(target.snapshot(),target)
    version = target.snapshot().version
    acknowledge_route_ask(target,NOW)
    assert target.snapshot().version == version


def test_batch_commits_once_recovers_and_deletes(tmp_path):
    async def run():
        import hashlib
        from types import SimpleNamespace
        from runtime.private_world.ledger import SQLitePrivateWorldLedger, LedgerEvent
        from runtime.memory.private_world_relationship import PrivateWorldRelationshipCommitter
        from runtime.relationship_batch import tick, BatchQueue
        from runtime.relationship_moments import moments, sync_messages
        target = SQLitePrivateWorldLedger(tmp_path / 'world.sqlite3')
        rows = []
        for i in range(50):
            row = {'letter_id':str(i),'private_world_delivery_id':f'delivery.{i}',
                   'private_world_occurred_at':NOW.isoformat(), 'content':f'今天练了第{i}首曲子', 'reply_text':f'第{i}首是你最喜欢的吗？'}
            target.apply_once(LedgerEvent(f'delivery.{i}',f'delivery.{i}','canonical_reply_delivered',
                {'canonical_reply_sha256':hashlib.sha256(row['reply_text'].encode()).hexdigest()},NOW.isoformat()), target.snapshot())
            rows.append(row)
        server = SimpleNamespace(private_world_relationship_committer=PrivateWorldRelationshipCommitter(target),
            store=SimpleNamespace(letters=rows,personal_chats=[]),letters_adapter=SimpleNamespace(gateway=None),
            _safe_log=lambda *a:None,_refresh_contact_relationship_projection=lambda:None)
        calls = []
        async def judge(gateway,batch,route):
            calls.append(batch['request_id'])
            return {'turns':[{'id':r['id'],'signals':{'rel_disclosure':'personal'},'moment':'rel_disclosure:personal',
                             'user_quote':r['user'],'reply_quote':r['reply']} for r in batch['turns']]}
        heartbeats=[]
        done=asyncio.Event()
        async def heartbeat():
            while not done.is_set():
                heartbeats.append(asyncio.get_running_loop().time())
                await asyncio.sleep(.005)
        task=asyncio.create_task(heartbeat())
        assert await tick(server,NOW,judge) == 'updated'
        done.set()
        await task
        assert len(heartbeats)>5  # Settlement must yield the chat event loop.
        assert max(b-a for a,b in zip(heartbeats,heartbeats[1:])) < .2
        assert target.snapshot().trust == 4
        assert len(moments(target,100)) == 50
        assert await tick(server,NOW,judge) == 'waiting' and len(calls)==1
        server.store.letters=[]
        sync_messages(target,[])
        assert not moments(target)
        assert await tick(server,NOW,judge) == 'waiting'


    asyncio.run(run())

def test_relationship_http_route_version_and_scope(tmp_path, monkeypatch):
    async def run():
        import local_server as server
        from types import SimpleNamespace
        from runtime.private_world.ledger import SQLitePrivateWorldLedger
        from runtime.memory.private_world_relationship import PrivateWorldRelationshipCommitter
        target = SQLitePrivateWorldLedger(tmp_path / 'world.sqlite3')
        monkeypatch.setattr(server,'private_world_relationship_committer',PrivateWorldRelationshipCommitter(target))
        monkeypatch.setattr(server,'daily_life_runtime',None)
        async def no_persist():
            pass
        monkeypatch.setattr(server,'_persist_store_state_async',no_persist)
        view = await server.route('GET','/toy/relationship',{}, {})
        assert view['code']==0 and len(view['data']['dimensions'])==4
        denied = await server.route('POST','/toy/relationship/route',{'route':'romantic','version':1},{})
        assert denied['code']==403
        chosen = await server.route('POST','/toy/relationship/route',{'route':'romantic','version':1},{},companion_confirmed=True)
        assert chosen['code']==0 and chosen['data']['route']=='romantic' and chosen['data']['volume']==1
        conflict = await server.route('POST','/toy/relationship/route',{'route':'family','version':1},{},companion_confirmed=True)
        assert conflict['code']==409
    asyncio.run(run())


def test_route_care_cadence_and_no_regression():
    from runtime.personal_chat.initiative_profile import profile_from_snapshot, profile_from_rows
    from runtime.relationship_growth import growth_updates, DEFAULTS
    state = PrivateWorldSnapshot(relationship_route='romantic', relationship_volume=4,
                                familiarity=70,comfort=70,trust=40,closeness=40)
    romance = profile_from_snapshot(state)
    friend = profile_from_snapshot(replace(state,relationship_route='friend'))
    family = profile_from_snapshot(replace(state,relationship_route='family'))
    assert romance.im_interval_min < family.im_interval_min < friend.im_interval_min
    restored = profile_from_rows([{'initiative_tier':romance.tier,'initiative_route':'romantic'}])
    assert restored.im_interval_min == romance.im_interval_min
    for _ in range(20):
        state = replace(state, **growth_updates(state,{'rel_friction':'conflict'},NOW,DEFAULTS))
    assert state.relationship_volume >= 4 and state.trust >= 35


def test_sensitive_moments_do_not_expose_quotes(tmp_path):
    from types import SimpleNamespace
    from runtime.private_world.ledger import SQLitePrivateWorldLedger
    from runtime.relationship_moments import record_moment
    from runtime.relationship_lite import details, writer_line
    target=SQLitePrivateWorldLedger(tmp_path/'world.sqlite3')
    with target._connection() as db:
        record_moment(db,'source',NOW.isoformat(),{'moment':'rel_disclosure:vulnerable',
                      'user_quote':'合成隐私内容','reply_quote':'谢谢你愿意告诉我'})
    server=SimpleNamespace(private_world_relationship_committer=SimpleNamespace(ledger=target),daily_life_runtime=None)
    assert details(server,NOW)['moments'][0]['quote']==''
    assert '合成隐私内容' not in writer_line(target.snapshot(),target)


def test_promise_completion_is_per_project(tmp_path):
    from runtime.private_world.daily_life import DailyLifeStore
    from runtime.relationship_moments import plans
    import json
    life=DailyLifeStore(tmp_path/'life.sqlite3')
    with life._db() as db:
        for identity,status in [('song','completed'),('walk','planned'),('invitation','awaiting_user')]:
            db.execute('INSERT INTO life_projects VALUES (?,?)',
                       (identity,json.dumps({'id':identity,'kind':'shared','status':status})))
    assert [p['id'] for p in plans(life)]==['walk']
