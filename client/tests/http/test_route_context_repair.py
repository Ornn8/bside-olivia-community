import asyncio
from types import SimpleNamespace

from letter_triage import LetterReplyRouter, RoutingContext
from runtime.diagnostics.support_bundle import _project_tail


def classify(**changes):
    arguments = dict(mode='text_letter', reason_code='direct_words', emotion_level='normal',
        music_contexts=['current_work_relevance'], music_role='none', music_intent='none',
        request_disposition='none', direct_response_sufficient=True,
        voice_materially_better=False, music_materially_better=False, character_willing=True)
    arguments.update(changes)
    arguments = {k: v for k, v in arguments.items() if v is not None}
    class Gateway:
        async def complete_with_tools(self, **kwargs):
            return [SimpleNamespace(name='select_reply_mode', arguments=arguments)]
    return asyncio.run(LetterReplyRouter(Gateway(), routing_context=RoutingContext(
        musical_video_available=True, voice_reply_available=True)).classify('synthetic'))


def test_unfounded_work_context_does_not_block_text():
    result = classify()
    assert result.status == 'completed'
    assert result.reply_mode == 'text_letter'
    assert result.music_contexts == ()


def test_unfounded_work_context_cannot_authorize_music():
    result = classify(mode='singing_video', music_role='performance', music_intent='perform',
        direct_response_sufficient=False, music_materially_better=True)
    assert result.status == 'unavailable'


def test_explicit_voice_request_survives_work_context_repair():
    result = classify(music_contexts=['current_work_relevance', 'explicit_voice_reply_request'])
    assert result.status == 'completed'
    assert result.reply_mode == 'voice_reply'
    assert result.music_contexts == ('explicit_voice_reply_request',)


def test_field_difference_survives_export_without_unknown_names():
    result = classify(mode=None, emotion_level=None, **{'private-secret-name': 'private-value'})
    assert result.status == 'unavailable'
    assert result.diagnostic['route_missing_fields'] == ['emotion_level', 'mode']
    assert result.diagnostic['route_extra_field_count'] == 1
    exported = _project_tail([{'event': 'reply_route_classification_failed', **result.diagnostic}], runtime=True)
    assert b'route_missing_fields' in exported and b'emotion_level' in exported
    assert b'route_extra_field_count' in exported
    assert b'private' not in exported
