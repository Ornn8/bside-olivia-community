import asyncio
import json
from types import SimpleNamespace

import pytest

from runtime import image_reply


def fixture(writer=None):
    calls = []
    async def describe(**kwargs):
        calls.append(kwargs)
        assert kwargs['tools'][0]['function']['name'] == 'describe_reply_photo'
        assert set(kwargs['tools'][0]['function']['parameters']['properties']) == {'prompt'}
        return [SimpleNamespace(name='describe_reply_photo', arguments={'prompt': 'A quiet evening portrait.'})]
    server = SimpleNamespace(letters_adapter=SimpleNamespace(gateway=SimpleNamespace(
        complete_with_tools=writer or describe)), _persist_store_state=lambda: None)
    reference = {'requested_image': True, 'world_current_location': 'bedroom',
                 'reply_as_of': '2026-09-28T02:00:00+08:00', 'expression_options': ['frustrated'],
                 'course_plan': {'attendance_confirmed': False},
                 'meal_records': [{'food': 'rice', 'status': 'planned', 'stale': True}]}
    return server, reference, calls


def test_frozen_image_uses_jev_choices_and_writer_has_no_semantic_fields():
    server, reference, calls = fixture()
    row = {}
    class Port:
        async def ask(self, state, questions, *, purpose):
            assert 'attach' not in questions
            assert 'cafe' not in questions['room']['criteria']
            assert purpose == 'reply_photo_plan'
            assert 'meal_records' not in state['reference']
            assert state['reference']['world_current_location'] == reference['world_current_location']
            return dict(photo_type='portrait', room='bedroom', time_of_day='night')
    result = asyncio.run(image_reply._jev_photo_plan(server, row, 'photo', 'Okay', reference, 'id', Port()))
    assert result == dict(attach=True, photo_type='portrait', room='bedroom', time_of_day='night',
                          prompt='A quiet evening portrait.')
    packet = json.loads(calls[0]['messages'][1]['content'])
    assert packet['reference'] == reference
    assert packet['frozen_photo_plan'] == row['image_semantic_plan']['plan']


def test_jev_failure_never_falls_back_to_photo_writer():
    server, reference, calls = fixture()
    class Port:
        async def ask(self, *args, **kwargs):
            raise ValueError('JEV_UNAVAILABLE')
    with pytest.raises(ValueError, match='JEV_UNAVAILABLE'):
        asyncio.run(image_reply._jev_photo_plan(server, {}, 'photo', 'Okay', reference, 'id', Port()))
    assert not calls


def test_unknown_location_offers_no_invented_preset_room():
    server, reference, _ = fixture()
    reference['world_current_location'] = None
    class Port:
        async def ask(self, state, questions, **kwargs):
            assert set(questions['room']['criteria']) == {'none'}
            return dict(photo_type='snapshot', room='none', time_of_day='night')
    result = asyncio.run(image_reply._jev_photo_plan(server, {}, 'photo', 'Okay', reference, 'id', Port()))
    assert result['room'] == 'none'


def test_writer_cannot_smuggle_a_second_attach_decision():
    async def writer(**kwargs):
        return [SimpleNamespace(name='describe_reply_photo', arguments={'prompt': 'x', 'attach': False})]
    server, reference, _ = fixture(writer)
    class Port:
        async def ask(self, *args, **kwargs):
            return dict(photo_type='portrait', room='none', time_of_day='night')
    with pytest.raises(ValueError, match='IMAGE_PLAN_INVALID'):
        asyncio.run(image_reply._jev_photo_plan(server, {}, 'photo', 'Okay', reference, 'id', Port()))


def test_prompt_retry_reuses_frozen_choices_but_changed_reply_is_rejected():
    server, reference, calls = fixture()
    row, decisions = {}, []
    class Port:
        async def ask(self, *args, **kwargs):
            decisions.append(True)
            return dict(photo_type='portrait', room='none', time_of_day='night')
    for _ in range(2):
        asyncio.run(image_reply._jev_photo_plan(server, row, 'photo', 'Okay', reference, 'id', Port()))
    assert len(decisions) == 1 and len(calls) == 2
    with pytest.raises(ValueError, match='IMAGE_GENERATION_BINDING_CHANGED'):
        asyncio.run(image_reply._jev_photo_plan(server, row, 'photo', 'changed', reference, 'id', Port()))
    assert len(calls) == 2


def test_unfrozen_legacy_row_can_be_declined_by_jev_without_writer():
    server, reference, calls = fixture()
    reference['requested_image'] = False
    class Port:
        async def ask(self, state, questions, **kwargs):
            assert 'attach' in questions
            return dict(attach='no', photo_type='snapshot', room='none', time_of_day='night')
    result = asyncio.run(image_reply._jev_photo_plan(server, {}, 'hello', 'Okay', reference, 'id', Port()))
    assert result['attach'] is False and not calls


def test_actual_photo_worker_uses_jev_path_and_preserves_error(monkeypatch):
    from runtime.reply import jev_questions
    from runtime import remote_generation
    server, reference, calls = fixture()
    server.video_reply_settings_store = SimpleNamespace(image_snapshot=lambda: {'enabled': True})
    class Port:
        async def ask(self, *args, **kwargs):
            raise ValueError('JEV_RESPONSE_INVALID')
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: Port())
    monkeypatch.setattr(image_reply, '_photo_reference', lambda *args: reference.copy())
    async def capabilities(*args):
        return {}
    monkeypatch.setattr(remote_generation, 'RemoteGeneration', lambda *args: SimpleNamespace(url='synthetic', token='test', request=capabilities))
    row = {'letter_id': 'synthetic'}
    asyncio.run(image_reply._prepare_once(server, row, 'photo', 'Okay'))
    assert row['image_status'] == 'FAILED'
    assert row['image_error_code'] == 'JEV_RESPONSE_INVALID'
    assert not calls


def test_photo_writer_keeps_light_posture_and_classroom_consistent_with_the_reply():
    """A 10:15 class photo came back dark, empty, with feet on the desk."""
    rules = image_reply._DESCRIPTION_SYSTEM
    assert 'time_of_day' in rules and '回信里描述的光线' in rules
    assert '不把脚搭在桌上' in rules and '不坐在桌子上' in rules
    assert '老师和正在听课的同学' in rules and '空无一人' in rules


def test_photo_reference_falls_back_to_last_observed_place_when_world_is_outdated():
    from datetime import datetime, timezone
    from runtime import image_reply
    from runtime.reply.character_emotion_context import freeze_expression_context, store_expression_context
    last = {'activity': '上心理学', 'evidence_kind': 'published_life', 'location': '学校',
            'occurred_at': '2026-09-29T06:00:00+00:00'}
    for world, expected in (
            ({'stale': True, 'current': None, 'last_observation': last}, '学校'),
            ({'stale': False, 'current': {**last, 'location': '琴房'}}, '琴房'),
            ({'stale': True, 'current': None}, None)):
        row = {'reply_text': '刚下课。'}
        snapshot = freeze_expression_context('r1', datetime(2026, 9, 29, 8, tzinfo=timezone.utc), world=world)
        store_expression_context(row, snapshot, row['reply_text'])
        reference = image_reply._photo_reference(row, row['reply_text'])
        assert reference['world_current_location'] == expected
        assert ('world_location_basis' in reference) == (world.get('last_observation') is not None)
