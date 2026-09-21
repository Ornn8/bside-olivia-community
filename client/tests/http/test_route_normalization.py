import asyncio
import json
from pathlib import Path

import pytest
from letter_triage import LetterReplyRouter, RoutingContext, explicitly_requested_route
from tests.http.test_letter_triage_portable import _Gateway

ROWS = json.loads(Path(__file__).with_name('route_matrix_fixture.json').read_text(encoding='utf-8'))

@pytest.mark.parametrize('row', ROWS, ids=lambda row: row['capability']+'-'+row['case'])
def test_captured_provider_matrix(row):
    voice = row['capability'] != 'text'
    music = row['capability'] == 'all'
    context = RoutingContext(voice_reply_available=voice, musical_video_available=music)
    gateway = _Gateway(row['arguments'][0])
    result = asyncio.run(LetterReplyRouter(gateway, routing_context=context).classify(row['content']))
    assert result.status == 'completed'
    assert len(gateway.requests) == 1
    if row['case'] not in {'voice', 'video', 'song', 'combined'}:
        assert explicitly_requested_route(result) is None
        assert result.request_disposition == 'none'
    if row['case'] == 'text_only' or row['capability'] == 'text':
        assert result.reply_mode == 'text_letter'
    if row['case'] in {'praise', 'music_discussion', 'past_request', 'capability_question'}:
        assert result.reply_mode not in {'singing_video', 'voice_song_video'}

@pytest.mark.parametrize('available,explicit,expected', [
    (False, False, 'text_letter'), (True, False, 'voice_reply'),
    (False, True, 'text_letter'), (True, True, 'voice_reply'),
])
def test_minimal_decision_projects_capability_and_request(available, explicit, expected):
    gateway = _Gateway(dict(mode='voice_reply', reason_code='synthetic', emotion_level='normal',
        music_role='none', music_contexts=['explicit_voice_reply_request'] if explicit else []))
    result = asyncio.run(LetterReplyRouter(gateway, routing_context=RoutingContext(
        voice_reply_available=available)).classify('synthetic'))
    assert result.status == 'completed'
    assert result.reply_mode == expected
    expected_disposition = ('fulfill' if available else 'defer') if explicit else 'none'
    assert result.request_disposition == expected_disposition
    assert len(gateway.requests[0]['tools'][0]['function']['parameters']['required']) == 5


def test_unavailable_automatic_motif_becomes_text_without_request():
    gateway = _Gateway(dict(mode='singing_video', reason_code='motif', emotion_level='normal',
        music_role='spontaneous_motif', music_contexts=['melody_idea']))
    result = asyncio.run(LetterReplyRouter(gateway, routing_context=RoutingContext()).classify('synthetic'))
    assert result.status == 'completed'
    assert result.reply_mode == 'text_letter'
    assert explicitly_requested_route(result) is None


def test_explicit_adaptation_survives_model_text_preference():
    gateway = _Gateway(dict(mode='text_letter', reason_code='adapt', emotion_level='normal',
        music_role='adaptation', music_contexts=['explicit_performance_or_adaptation_request']))
    result = asyncio.run(LetterReplyRouter(gateway, routing_context=RoutingContext(True)).classify('synthetic'))
    assert result.status == 'completed'
    assert result.reply_mode == 'singing_video'
    assert result.music_intent == 'adapt'


def test_automatic_route_setting_is_enforced_by_program():
    gateway = _Gateway(dict(mode='voice_reply', reason_code='voice', emotion_level='normal',
        music_role='none', music_contexts=[]))
    result = asyncio.run(LetterReplyRouter(gateway, routing_context=RoutingContext(
        voice_reply_available=True, automatic_routes=())).classify('synthetic'))
    assert result.status == 'completed'
    assert result.reply_mode == 'text_letter'


@pytest.mark.parametrize('change', [
    {'mode': 'unknown'}, {'mode': []}, {'music_role': {}},
    {'music_contexts': ['invented']}, {'music_contexts': ['melody_idea', 'melody_idea']},
    {'extra': 'unexpected'}, {'emotion_level': None},
])
def test_minimal_decision_still_rejects_corrupt_fields(change):
    args = dict(mode='text_letter', reason_code='synthetic', emotion_level='normal',
                music_role='none', music_contexts=[])
    args.update(change)
    result = asyncio.run(LetterReplyRouter(_Gateway(args), routing_context=RoutingContext()).classify('synthetic'))
    assert result.status == 'unavailable'
    assert result.reason_code == 'router_invalid_result'
