"""Copied source headers must not become a delivered reply or spoken script."""
import json
from datetime import datetime, timezone

import pytest

from runtime.personal_chat.decision import decode
from tests.http.test_expression_context_routes import run_isolated


BODY = '收到啦。你说的[当前消息]我看到了。'


@pytest.fixture(params=['当前消息', '历史消息'])
def header(request):
    metadata = dict(source='received-user:letter:current',
                    event_id='received-user:letter:current', actor='user',
                    evidence_kind='current_input', channel='letter',
                    received_at='2026-10-10T15:00:00+08:00', truncated=False)
    return '[' + request.param + ' ' + json.dumps(metadata) + ']\n'


def test_chat_removes_provenance_but_preserves_reply_and_rejects_header_only(header):
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    raw = json.dumps(dict(text=header + BODY, delivery='voice'))
    assert decode(raw, user='收到吗', now=now)['text'] == BODY
    with pytest.raises(ValueError):
        decode(json.dumps(dict(text=header, delivery='voice')), user='收到吗', now=now)


@pytest.mark.parametrize('body', [BODY, ''])
def test_letter_removes_provenance_before_save_memory_and_media(tmp_path, header, body):
    run_isolated(tmp_path, 'raw_reply = ' + repr(header + body) + '\nbody = ' + repr(body) + '\n' + r'''
import asyncio
import local_server as server
from runtime import image_reply
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer
from tests.persona.test_jev_pipeline import Port, plan
from tests.persona.test_reply_semantic_wiring import Engine

row = {'letter_id': 'synthetic-provenance', 'content': '收到吗',
       'reply_routes': {'voice_reply': True, 'singing_video': False, 'voice_song_video': False},
       'image_reply_settings': {'enabled': False}}
server.store.letters[:] = [row]
server.store.personal_chats[:] = []
server.daily_life_runtime = None
server._current_life_rhythm = lambda: {}
server._schedule_text_reply_delay = lambda *args: None
server._commit_private_world_letter = lambda row: False
remembered, media, photos = [], [], []
server.letters_adapter.remember_conversation = lambda content, text: remembered.append(text)
server._schedule_media_job = lambda letter_id, content, text, mode: media.append((text, mode))
image_reply.schedule = lambda server, row: photos.append(row['reply_text'])
server.reply_pipeline = ReplyPipeline(Engine(raw_reply), reviewer=NullReviewer(),
    rewriter=UnavailableRewriter(), discover_runtime_ports=False,
    companion_decision_port=Port(plan(kind='audio_speech')))

assert asyncio.run(server.generate_reply(row['letter_id'], row['content'])) is bool(body)
if body:
    assert row['reply_text'] == body
    assert remembered == photos == [body]
    assert media == [(body, 'voice_reply')]
else:
    assert row['letter_status'] == 'FAILED' and row['error_code'] == 'LLM_PROTOCOL_ERROR'
    assert not row.get('reply_text') and not remembered and not media and not photos
server.store.letters.clear()
server._load_store_state()
assert server.store.letters[0].get('reply_text', '') == body
''')
