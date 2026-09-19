import asyncio
import json
from types import SimpleNamespace

import pytest


def test_natural_chunks_prefer_sentence_end_and_preserve_text():
    from tts.external_breeze_worker import _audio_text_chunks
    text = '甲' * 110 + '。' + '乙' * 50 + '，' + '丙' * 100 + '。'
    chunks = _audio_text_chunks(text)
    assert chunks[0] == '甲' * 110 + '。'
    assert ''.join(chunks) == text
    assert max(map(len, chunks)) <= 180
    assert _audio_text_chunks('短句。') == ['短句。']


def test_progress_survives_completion_without_private_values(tmp_path):
    from tts.external_breeze_worker import _write_status, project_worker_status
    path = tmp_path/'status.json'
    _write_status(path, {'phase':'generation', 'chunk_count':3, 'chunk_index':2,
        'generated_frames':500, 'max_frames':500, 'limit_reached':True,
        'instruction_enabled':False, 'text':'private', 'path':'private'})
    _write_status(path, {'phase':'completed'})
    result = project_worker_status(json.loads(path.read_text()))
    assert result['chunk_count'] == 3 and result['limit_reached'] is True
    assert 'private' not in json.dumps(result)
    assert not project_worker_status({'chunk_count':True,'elapsed_seconds':float('nan')})


def test_live_worker_is_exportable_without_error_log(tmp_path):
    from original_client_server import _media_provider_tail
    path=tmp_path/'media/olivia-voice-test/olivia-delivery-test/worker-status.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'phase':'generation','chunk_count':4,'chunk_index':2,'text':'private'}))
    records=_media_provider_tail(tmp_path)
    assert len(records)==1
    assert json.loads(records[0]['diagnostic'])['worker']['chunk_index']==2
    assert 'private' not in str(records)


@pytest.mark.parametrize('status,body,quota', [
    (402,{},True),(429,{'error':{'code':'insufficient_quota'}},True),
    (429,{'error':{'code':'rate_limit_exceeded'}},False),(401,{},False),
])
def test_quota_is_distinct_from_throttling(status,body,quota):
    from llm_gateway import _check_provider_quota, GatewayError
    async def text():return json.dumps(body)
    response=SimpleNamespace(status=status,text=text)
    if quota:
        with pytest.raises(GatewayError,match='PROVIDER_QUOTA_EXHAUSTED'):
            asyncio.run(_check_provider_quota(response))
    else:
        asyncio.run(_check_provider_quota(response))
