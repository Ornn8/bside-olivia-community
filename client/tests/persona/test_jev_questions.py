import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace
import pytest

from runtime.reply.jev_questions import JevQuestionsPort
from runtime.reply.jev_semantic_service import decide


@pytest.mark.parametrize('reason,expected', [
    ('invalid_plan_contract', 'JEV_PLAN_CONTRACT_INVALID'),
    ('provider_http_503', 'JEV_PROVIDER_HTTP_503'),
    ('PRIVATE_PROVIDER_DETAIL', 'JEV_HTTP_503'),
])
def test_semantic_transport_preserves_safe_failure_category(reason, expected):
    import io
    from urllib.error import HTTPError
    calls = []
    body = io.BytesIO(json.dumps({'error': reason, 'detail': 'PRIVATE_PROVIDER_DETAIL'}).encode())
    class FailingOpener:
        def open(self, request, **kwargs):
            calls.append(request)
            raise HTTPError(request.full_url, 503, 'unavailable', {}, body)
    port = JevQuestionsPort('http://127.0.0.1:8097/v1/companion/decide')
    port.transport._opener = FailingOpener()
    with pytest.raises(ValueError) as failure:
        port.ask_sync({'text': 'test'}, {'q': {'instructions': 'Choose', 'criteria': {'ok': 'OK'}}}, purpose='test')
    assert str(failure.value) == expected
    assert 'PRIVATE_PROVIDER_DETAIL' not in str(failure.value)
    assert len(calls) == 1 and body.closed


def test_semantic_request_accepts_complete_30kb_evidence_and_rejects_over_32kb():
    from runtime.reply.jev_questions import SEMANTIC_REQUEST_MAX_BYTES
    packet = {'state': {'evidence': 'x' * 30000}, 'purpose': 'quality_test',
              'questions': {'q': {'instructions': 'Choose', 'criteria': {'ok': 'OK'}}}}
    assert decide(SimpleNamespace(), packet)['decisions'] == {'q': 'ok'}
    packet['state']['evidence'] = 'x' * SEMANTIC_REQUEST_MAX_BYTES
    with pytest.raises(ValueError, match='invalid_body_size'):
        decide(object(), packet)
    port = JevQuestionsPort('http://127.0.0.1:1/v1/companion/decide')
    with pytest.raises(ValueError, match='JEV_INPUT_TOO_LARGE'):
        asyncio.run(port.ask(packet['state'], packet['questions'], purpose=packet['purpose']))


def test_error_body_interruption_preserves_http_category_and_closes_response():
    from http.client import IncompleteRead
    from urllib.error import HTTPError
    class Body:
        closed = False
        def read(self, *args): raise IncompleteRead(b'private partial body', 100)
        def close(self): self.closed = True
    body = Body()
    class Opener:
        def open(self, request, **kwargs):
            raise HTTPError(request.full_url, 503, 'unavailable', {}, body)
    port = JevQuestionsPort('http://127.0.0.1:8097/v1/companion/decide')
    port.transport._opener = Opener()
    with pytest.raises(ValueError, match='^JEV_HTTP_503$') as failure:
        port.ask_sync({'text': 'synthetic'}, {'q': {'instructions': 'Choose', 'criteria': {'yes': 'yes'}}},
            purpose='test')
    assert body.closed and failure.value.failure_context['http_status'] == 503


def test_native_choice_bridge_binds_input_and_rejects_altered_result():
    class Native:
        def ask(self, state, questions):
            assert state == {'text': '我需要休息'}
            assert questions['reaction']['type'] == 'choice'
            return {'reaction': {'choice':'rest'}}
    tamper = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            assert self.path == '/v1/companion/semantic-decisions'
            assert self.headers['Authorization'] == 'Bearer synthetic'
            packet = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            value = dict(decide(Native(), packet), backend='jev')
            if tamper:
                value['input_digest'] = 'wrong'
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = JevQuestionsPort(f'http://127.0.0.1:{server.server_port}/v1/companion/decide', token='synthetic')
        async def run():
            return await port.ask({'text':'我需要休息'}, {'reaction':dict(instructions='判断行动',criteria={'rest':'休息','continue':'继续'})}, purpose='test-emotion')
        assert asyncio.run(run()) == {'reaction':'rest'}
        tamper.append(True)
        with pytest.raises(ValueError, match='JEV_RESPONSE_INVALID'):
            asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize('failure,expected,stage', [
    (TimeoutError('private address'), 'JEV_TIMEOUT', 'request'),
    (__import__('urllib.error', fromlist=['URLError']).URLError(TimeoutError('private address')),
     'JEV_TIMEOUT', 'request'),
    (__import__('http.client', fromlist=['RemoteDisconnected']).RemoteDisconnected('private body'),
     'JEV_UNAVAILABLE', 'request'),
])
def test_transport_failure_is_categorized_and_traceable_without_private_text(failure, expected, stage):
    from runtime.diagnostics.failure_context import failure_snapshot
    from uuid import UUID
    calls = []
    class Opener:
        def open(self, request, **kwargs):
            calls.append(request)
            raise failure
    port = JevQuestionsPort('http://127.0.0.1:8097/v1/companion/decide')
    port.transport._opener = Opener()
    with pytest.raises(ValueError, match=expected):
        port.ask_sync({'text': 'private input'}, {'q': {'instructions': 'Choose',
            'criteria': {'yes': 'yes', 'no': 'no'}}}, purpose='test')
    evidence = failure_snapshot()[-1]
    assert evidence['cause_code'] == expected and evidence['failure_stage'] == stage
    assert str(UUID(evidence['provider_request_id'])) == calls[0].get_header('X-olivia-request-id')
    assert 'private' not in json.dumps(evidence)
