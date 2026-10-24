"""Her voice starts while the reply is reviewed; it is sent only when the reviewed words and medium match (2.5.0)."""
import asyncio

from runtime.personal_chat import backend
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.presentation import CURRENT
from tests.http.test_chat_jev_decision import ordinary_chat_result, server_fixture

WORDS = '今天练得挺顺，刚歇下来。'


def turn(monkeypatch, tmp_path, *, early, delivery='audio_speech', result=None, slow=False):
    """early: the words the writer first wrote (the voice starts on them); result: the reviewed reply."""
    started = []
    hook = {}
    async def run_hook():
        preview = CURRENT.get().get('voice_preview')
        hook['offered'] = preview is not None
        if preview is not None:
            preview({'text': early, 'delivery': 'voice', 'text_reason': None}, delivery, False)
            started.extend(backend._VOICE_PREVIEWS)
            await asyncio.sleep(0)
    server, row, _, _, audio = server_fixture(monkeypatch, tmp_path, result or ordinary_chat_result(delivery='voice'),
                                              run_hook=run_hook)
    if slow:
        async def never(server, text, path, **kwargs):
            audio.append(text)
            await asyncio.Event().wait()
        monkeypatch.setattr(backend, 'prepare_chat_audio', never)
    text = asyncio.run(backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', '今天怎么样？'), row))
    return text, row, audio, started, hook


def test_the_voice_started_during_the_review_is_the_one_sent(monkeypatch, tmp_path):
    _, row, audio, started, _ = turn(monkeypatch, tmp_path, early=WORDS)
    assert started and audio == [WORDS]  # rendered once, before the review ended
    assert row['voice_preview'] == 'USED' and row['prepared_audio'] and row['requested_format'] == 'voice'
    assert backend._VOICE_PREVIEWS == {}


def test_words_changed_by_the_review_get_their_own_voice(monkeypatch, tmp_path):
    _, row, audio, _, _ = turn(monkeypatch, tmp_path, early='今天练得一般。')
    assert audio == ['今天练得一般。', WORDS] and row['voice_preview'] == 'CHANGED'
    assert backend._VOICE_PREVIEWS == {}


def test_a_reply_that_goes_as_text_never_sends_the_early_voice(monkeypatch, tmp_path):
    result = ordinary_chat_result(delivery='text', text_reason='speaker_unavailable')
    _, row, audio, started, _ = turn(monkeypatch, tmp_path, early=WORDS, result=result, slow=True)
    assert started and audio == [WORDS] and row['requested_format'] == 'text'
    assert 'prepared_audio' not in row and backend._VOICE_PREVIEWS == {}


def test_only_a_reply_meant_as_voice_starts_one(monkeypatch, tmp_path):
    for delivery in ('image', 'video_speech', None):
        _, _, audio, started, hook = turn(monkeypatch, tmp_path, early=WORDS, delivery=delivery)
        assert hook['offered'] and not started
    seen = []  # no voice transport: nothing to start
    async def run_hook():
        seen.append('voice_preview' in CURRENT.get())
    server, row, *_ = server_fixture(monkeypatch, tmp_path, ordinary_chat_result(delivery='voice'), run_hook=run_hook)
    row['voice_available'] = False
    asyncio.run(backend.generate(server, PersonalMessage('qq', 'b', 'u', '1', '今天怎么样？'), row))
    assert seen == [False]
