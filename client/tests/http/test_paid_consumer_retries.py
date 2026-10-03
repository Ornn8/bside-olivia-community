"""Paid extraction stops durably; local persistence can recover without regeneration."""
import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from runtime import image_understanding as images
from runtime.personal_chat import backend
from llm_gateway import GatewayConfig, OpenAICompatibleAdapter
from private_world_candidate import (
    CandidateDeliveryStatus, GatewayPrivateWorldCandidateAnalyzer,
    PrivateWorldCandidateProposal, PrivateWorldCandidateRequest, deliver_private_world_candidate,
)
from private_world_candidates import CandidateType, CandidateWriteStatus


def photo_row():
    return dict(letter_id='synthetic-photo', image_delivery_status='DELIVERED',
                prepared_image='synthetic.png')


def server_for(row):
    persisted = []
    return SimpleNamespace(_persist_store_state=lambda: persisted.append(json.loads(json.dumps(row)))), persisted


@pytest.mark.parametrize('error,expected_calls,status', [
    (TimeoutError('synthetic'), 3, 'EXHAUSTED'),
    (ValueError('IMAGE_VISION_INVALID'), 1, 'TERMINAL_REJECTION'),
    (ValueError('IMAGE_VISION_INCOMPLETE'), 1, 'TERMINAL_REJECTION'),
    (RuntimeError('IMAGE_VISION_DUPLICATE'), 1, 'TERMINAL_REJECTION'),
])
def test_photo_failure_stops_after_restart(monkeypatch, error, expected_calls, status):
    async def run():
        row = photo_row()
        server, persisted = server_for(row)
        calls = []

        async def describe(*args, **kwargs):
            calls.append(1)
            assert persisted[-1].get('image_description_attempts') == len(calls)
            raise error

        monkeypatch.setattr(images, 'describe_image', describe)
        for index in range(10):
            row['image_description_retry_at'] = 0
            await images.commit_image_memory(server, row)
            if index == 1:
                row = json.loads(json.dumps(row))
                server, persisted = server_for(row)
        assert len(calls) == expected_calls
        assert row['image_description_retry_status'] == status
        assert row['image_world_status'] == 'PENDING'
        assert 'image_description_retry_at' not in row

    asyncio.run(run())


def test_existing_exhausted_photo_does_not_restart_paid_work(monkeypatch):
    async def run():
        row = dict(photo_row(), image_description_failures=12, image_description_retry_at=0)
        server, _ = server_for(row)
        calls = []

        async def describe(*args, **kwargs):
            calls.append(1)
            raise TimeoutError()

        monkeypatch.setattr(images, 'describe_image', describe)
        await images.commit_image_memory(server, row)
        assert not calls
        assert row['image_description_retry_status'] == 'EXHAUSTED'

    asyncio.run(run())


def test_saved_photo_description_only_retries_local_world(monkeypatch):
    async def run():
        row = photo_row()
        server, _ = server_for(row)
        model_calls, world_calls = [], []

        async def describe(*args, **kwargs):
            model_calls.append(1)
            return dict(source='generated', summary='蓝色方块', sha256='a' * 64,
                        observed_at=datetime.now(timezone.utc).isoformat(), evidence_kind='visual_observation')

        def record(item):
            world_calls.append(item)
            if len(world_calls) < 3:
                raise OSError('synthetic storage failure')

        monkeypatch.setattr(images, 'describe_image', describe)
        server.daily_life_runtime = SimpleNamespace(store=SimpleNamespace(record_image_observation=record))
        await images.commit_image_memory(server, row)
        row = json.loads(json.dumps(row))
        for _ in range(3):
            await images.commit_image_memory(server, row)
        assert len(model_calls) == 1
        assert len(world_calls) >= 3
        assert row['image_world_status'] == 'COMMITTED'

    asyncio.run(run())


def test_photo_rpc_uses_stable_operation_key_and_duplicate_is_terminal(tmp_path, monkeypatch):
    async def run():
        from PIL import Image
        path = tmp_path / 'picture.png'
        Image.new('RGB', (16, 16), 'blue').save(path)
        seen = []

        async def handler(request):
            seen.append(request.headers.get('Idempotency-Key'))
            return web.json_response({'error': {'code': 'request_already_submitted'}}, status=409)

        app = web.Application()
        app.router.add_post('/v1/chat/completions', handler)
        async with TestClient(TestServer(app)) as endpoint:
            monkeypatch.setattr(images, '_vision_connection', lambda server: (str(endpoint.make_url('/v1')).rstrip('/'), 'synthetic'))
            for _ in range(2):
                with pytest.raises(RuntimeError, match='IMAGE_VISION_DUPLICATE'):
                    await images.describe_image(SimpleNamespace(), path, operation_id='synthetic-delivery')
        assert seen[0] == seen[1]
        assert seen[0] and len(seen[0]) <= 100

    asyncio.run(run())


def candidate_request():
    return PrivateWorldCandidateRequest('synthetic-candidate', 1, 'synthetic input', 'synthetic reply',
                                       SimpleNamespace(to_dict=lambda: {}), datetime.now(timezone.utc))


def test_candidate_transient_failure_has_durable_budget():
    async def run():
        checkpoint, calls, persisted = {}, [], []

        class Analyzer:
            async def analyze(self, request):
                calls.append(1)
                assert persisted[-1]['candidate_analysis_attempts'] == len(calls)
                raise TimeoutError('synthetic transport failure')

        for index in range(10):
            checkpoint['candidate_analysis_retry_at'] = 0
            await deliver_private_world_candidate(Analyzer(), SimpleNamespace(), candidate_request(),
                checkpoint=checkpoint, persist=lambda: persisted.append(json.loads(json.dumps(checkpoint))))
            if index == 1:
                checkpoint = json.loads(json.dumps(checkpoint))
        assert len(calls) == 3
        assert checkpoint['candidate_analysis_retry_status'] == 'EXHAUSTED'

    asyncio.run(run())


def test_invalid_candidate_response_is_terminal_after_restart(monkeypatch):
    async def run():
        calls = []

        async def complete(*args, **kwargs):
            calls.append(1)
            return SimpleNamespace(text='{}')

        monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: None)
        analyzer = GatewayPrivateWorldCandidateAnalyzer(SimpleNamespace(complete=complete), timeout_seconds=1)
        checkpoint = {}
        for _ in range(6):
            checkpoint['candidate_analysis_retry_at'] = 0
            await deliver_private_world_candidate(analyzer, SimpleNamespace(), candidate_request(),
                checkpoint=checkpoint, persist=lambda: None)
            checkpoint = json.loads(json.dumps(checkpoint))
        assert len(calls) == 1
        assert checkpoint['candidate_analysis_retry_status'] == 'TERMINAL_REJECTION'

    asyncio.run(run())


@pytest.mark.parametrize('code,expected_calls,status', [
    ('JEV_UNAVAILABLE', 3, 'EXHAUSTED'),
    ('JEV_TIMEOUT', 3, 'EXHAUSTED'),
    ('JEV_HTTP_429', 3, 'EXHAUSTED'),
    ('JEV_PROVIDER_HTTP_503', 3, 'EXHAUSTED'),
    ('JEV_RESPONSE_INVALID', 1, 'TERMINAL_REJECTION'),
    ('JEV_BALANCE_INSUFFICIENT', 1, 'TERMINAL_REJECTION'),
])
def test_jev_candidate_classifies_transient_and_terminal_failures(monkeypatch, code, expected_calls, status):
    async def run():
        calls = []

        class Questions:
            async def ask(self, *args, **kwargs):
                calls.append(1)
                raise ValueError(code)

        async def forbidden_text_fallback(*args, **kwargs):
            raise AssertionError('JEV failure must not call a text model')

        monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: Questions())
        analyzer = GatewayPrivateWorldCandidateAnalyzer(SimpleNamespace(complete=forbidden_text_fallback), timeout_seconds=1)
        checkpoint = {}
        for _ in range(6):
            checkpoint['candidate_analysis_retry_at'] = 0
            await deliver_private_world_candidate(analyzer, SimpleNamespace(), candidate_request(),
                checkpoint=checkpoint, persist=lambda: None)
            checkpoint = json.loads(json.dumps(checkpoint))
        assert len(calls) == expected_calls
        assert checkpoint['candidate_analysis_retry_status'] == status

    asyncio.run(run())


def test_candidate_store_failure_reuses_saved_analysis_after_restart():
    async def run():
        calls, writes = [], []
        checkpoint = {}

        class Analyzer:
            async def analyze(self, request):
                calls.append(1)
                return PrivateWorldCandidateProposal(CandidateType.BOUNDARY_RESPECTED, .8, 'synthetic evidence')

        def add(candidate):
            writes.append(candidate)
            if len(writes) <= 4:
                raise OSError('synthetic storage failure')
            return CandidateWriteStatus.CREATED

        result = None
        for _ in range(5):
            result = await deliver_private_world_candidate(Analyzer(), SimpleNamespace(add=add), candidate_request(),
                checkpoint=checkpoint, persist=lambda: None)
            checkpoint = json.loads(json.dumps(checkpoint))
        assert len(calls) == 1 and len(writes) == 5
        assert result is CandidateDeliveryStatus.CREATED

    asyncio.run(run())


def test_candidate_no_paid_call_before_checkpoint_persists():
    async def run():
        calls = []

        class Analyzer:
            async def analyze(self, request):
                calls.append(1)
                return None

        def persist():
            raise OSError('synthetic persistence failure')

        await deliver_private_world_candidate(Analyzer(), SimpleNamespace(), candidate_request(),
                                             checkpoint={}, persist=persist)
        assert not calls

    asyncio.run(run())


def test_candidate_gateway_uses_same_billing_key_after_adapter_restart():
    config = GatewayConfig(provider='openai_compatible', base_url='https://synthetic.invalid/v1', model='synthetic')
    first = OpenAICompatibleAdapter(config)._headers('synthetic', 'private-world-candidate:x:1')
    restarted = OpenAICompatibleAdapter(config)._headers('synthetic', 'private-world-candidate:x:1')
    revised = OpenAICompatibleAdapter(config)._headers('synthetic', 'private-world-candidate:x:2')
    assert first.get('Idempotency-Key')
    assert first['Idempotency-Key'] == restarted['Idempotency-Key']
    assert revised['Idempotency-Key'] != first['Idempotency-Key']


def test_concurrent_candidate_consumers_share_one_analysis():
    async def run():
        checkpoint, calls = {}, []

        class Analyzer:
            async def analyze(self, request):
                calls.append(1)
                await asyncio.sleep(.01)
                return None

        results = await asyncio.gather(*[
            deliver_private_world_candidate(Analyzer(), SimpleNamespace(), candidate_request(),
                                            checkpoint=checkpoint, persist=lambda: None)
            for _ in range(2)
        ])
        assert calls == [1]
        assert results == [CandidateDeliveryStatus.SKIPPED] * 2

    asyncio.run(run())


def test_local_checkpoint_failure_does_not_consume_model_budget():
    async def run():
        checkpoint, calls, saves = {}, [], []

        class Analyzer:
            async def analyze(self, request):
                calls.append(1)
                return None

        def persist():
            saves.append(1)
            if len(saves) < 5:
                raise OSError('synthetic local storage outage')

        for _ in range(5):
            result = await deliver_private_world_candidate(Analyzer(), SimpleNamespace(), candidate_request(),
                                                           checkpoint=checkpoint, persist=persist)
        assert calls == [1] and result is CandidateDeliveryStatus.SKIPPED
        assert checkpoint['candidate_analysis_attempts'] == 1

    asyncio.run(run())


def test_failed_analysis_diagnostics_exclude_cached_private_facts():
    from runtime.diagnostics.photo import project_photo
    from runtime.diagnostics.support_bundle import project_chat_task
    row = dict(candidate_analysis_result={'summary': 'PRIVATE_FACT_NEVER_EXPORT'},
               candidate_analysis_retry_status='TERMINAL_REJECTION', candidate_analysis_attempts=1,
               candidate_analysis_failure_reason='PRIVATE_WORLD_CANDIDATE_ANALYSIS_UNAVAILABLE',
               image_description={'summary': 'PRIVATE_PICTURE_NEVER_EXPORT'},
               image_description_retry_status='EXHAUSTED', image_description_attempts=3,
               image_description_failure_reason='IMAGE_VISION_UNAVAILABLE')
    projected = project_chat_task(row) | project_photo(row)
    assert projected['candidate_analysis_retry_status'] == 'TERMINAL_REJECTION'
    assert projected['image_description_retry_status'] == 'exhausted'
    assert 'PRIVATE_FACT' not in json.dumps(projected)
    assert 'PRIVATE_PICTURE' not in json.dumps(projected)


def test_malformed_analysis_metadata_does_not_break_diagnostic_export():
    from runtime.diagnostics.photo import project_photo
    from runtime.diagnostics.support_bundle import project_chat_task
    row = dict(candidate_analysis_status={}, candidate_analysis_retry_status=[],
               candidate_analysis_failure_reason={'secret': 'PRIVATE_FACT_NEVER_EXPORT'},
               image_description_retry_status=[], image_description_failure_reason={})
    assert project_chat_task(row) | project_photo(row) == {}


@pytest.mark.parametrize('saved', [{}, {'candidate_type': 'conflict', 'confidence': float('nan'), 'summary': 'synthetic'}])
def test_invalid_saved_candidate_never_pays_to_reconstruct_it(saved):
    async def run():
        calls = []

        class Analyzer:
            async def analyze(self, request):
                calls.append(1)
                return None

        checkpoint = dict(candidate_analysis_result=saved)
        result = await deliver_private_world_candidate(Analyzer(), SimpleNamespace(), candidate_request(),
                                                       checkpoint=checkpoint, persist=lambda: None)
        assert not calls and result is CandidateDeliveryStatus.UNAVAILABLE
        assert checkpoint['candidate_analysis_retry_status'] == 'TERMINAL_REJECTION'
        assert checkpoint['candidate_analysis_failure_reason'] == 'PRIVATE_WORLD_CANDIDATE_CACHE_INVALID'

    asyncio.run(run())


def test_exhausted_candidate_does_not_block_local_memory_recovery():
    async def run():
        row = dict(letter_id='synthetic', delivery_status='DELIVERED', content='synthetic', reply_text='reply',
                   private_world_status='COMMITTED', daily_life_status='COMMITTED',
                   consumer_failures=14, consumer_error_code='PERSONAL_CHAT_CANDIDATE_UNAVAILABLE')
        calls, memory = [], []

        async def deliver(*args):
            calls.append(1)
            return CandidateDeliveryStatus.UNAVAILABLE

        server = SimpleNamespace(private_world_candidate_store=object(), _deliver_private_world_candidate=deliver,
            _persist_store_state=lambda: None, _safe_log=lambda *args, **kwargs: None,
            letters_adapter=SimpleNamespace(remember_conversation=lambda *args: memory.append(1)))
        await backend.recoverable_commit(server, row)
        assert not calls
        assert memory == [1] and row['legacy_memory_delivered']
        assert row['candidate_analysis_retry_status'] == 'EXHAUSTED'
        assert row['delivery_status'] == 'DELIVERED'

    asyncio.run(run())
