from __future__ import annotations

import io
import json
import zipfile

import pytest

from runtime.diagnostics.support_bundle import (
    DiagnosticBundleError,
    build_diagnostic_bundle,
)


def _source() -> dict[str, object]:
    return {
        "summary": {
            "status": "available",
            "backend_id": "desktop-local",
            "contract_version": "2.0",
            "python_version": "3.12.4",
            "os_name": "Windows",
            "os_release": "11",
            "architecture": "AMD64",
            "untrusted": "ignore me",
        },
        "health": {
            "status": "available",
            "checks": {"memory": {"state": "available", "real_id": "user-123"}},
        },
        "install": {
            "status": "available",
            "setup_completed": True,
            "key_configured": True,
            "absolute_path": r"C:\\Users\\pc",
        },
        "tasks": {
            "status": "active",
            "pending": 1,
            "items": [
                {
                    "status": "failed",
                    "error_code": "LLM_TIMEOUT",
                    "media_status": "not_requested",
                    "reply_mode": "text_letter",
                    "retryable": True,
                    "stage": "failed",
                    "elapsed_bucket": "5m_15m",
                    "real_id": "task-77",
                    "body": "private reply",
                }
            ],
        },
        "launcher_tail": [
            {
                "event": "backend_ready",
                "attempt": 1,
                "path": r"C:\\Users\\pc\\secret",
                "url": "https://private.example/token",
            }
        ],
        "runtime_tail": [
            {
                "event": "letter_completed",
                "reply_mode": "text",
                "path": "/toy/letter/private-real-id",
                "body": "never include this",
            }
        ],
    }


def test_transport_close_and_interruption_survive_bundle_without_secrets(monkeypatch, capsys):
    from collections import deque
    import local_server
    source = _source()
    source['runtime_tail'] = [dict(event='personal_chat_transport_closed', channel='qq',
        recorded_at_ms=1791127848000, close_code=4001, processing=True, pending_actions=0,
        response_queue=1, intake_queue=0, control_queue=0, transport_error='NONE',
        account='private-account', owner='private-owner', reason_text='private-close-detail', token='private-token'),
        dict(event='personal_chat_transport_state', channel='qq', status='connected', recorded_at_ms=1791127849000),
        dict(event='personal_chat_exchange_cancelled', channel='qq',
             error_code='PERSONAL_CHAT_GENERATION_INTERRUPTED', recorded_at_ms=1791127848001)]
    # Exercise the actual in-memory log ring as well as the ZIP projection.
    monkeypatch.setattr(local_server, '_RUNTIME_DIAGNOSTIC_EVENTS', deque(maxlen=160))
    monkeypatch.setattr(local_server, '_RUNTIME_REQUEST_EVENTS', deque(maxlen=40))
    for row in source['runtime_tail']:
        local_server._safe_log(row['event'], **{key: value for key, value in row.items() if key != 'event'})
    source['runtime_tail'] = list(local_server.runtime_diagnostic_event_snapshot())
    capsys.readouterr()
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        raw = archive.read('runtime-tail.jsonl').decode()
    rows = [json.loads(line) for line in raw.splitlines()]
    assert rows[0] == dict(event='personal_chat_transport_closed', channel='qq',
        recorded_at_ms=1791127848000, close_code=4001, processing=True, pending_actions=0,
        response_queue=1, intake_queue=0, control_queue=0, transport_error='NONE')
    assert rows[1:] == source['runtime_tail'][1:]
    assert 'private-' not in raw


def test_chat_order_evidence_survives_two_projections_without_private_identifiers():
    from runtime.diagnostics.support_bundle import project_chat_task
    raw = {
        'channel': 'qq', 'letter_id': 'im-' + 'abcdef01' * 8,
        'delivery_status': 'DELIVERED', 'generation_attempts': 2,
        'life_received_at': '2026-09-26T14:39:00+08:00',
        'user_sent_at': '2026-09-26T14:38:00+08:00',
        'private_world_occurred_at': '2026-09-26T06:39:07+00:00',
        'source_messages': {'651930410': 'private-message'},
        'account_id': '651930410', 'reply_text': 'private-reply', 'key': 'sk-private',
    }
    projected = project_chat_task(raw)
    assert projected['timeline'] == {
        'platform_sent_at': '2026-09-26T06:38:00Z',
        'processing_started_at': '2026-09-26T06:39:00Z',
        'delivered_at': '2026-09-26T06:39:07Z',
    }
    assert projected['turn_ref'].startswith('chat-')
    assert projected['generation_attempts'] == 2
    assert project_chat_task(projected) == projected
    source = _source()
    source['tasks']['items'][0].update(projected)
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        encoded = archive.read('tasks.json')
    exported = json.loads(encoded)['items'][0]
    assert exported['turn_ref'] == projected['turn_ref']
    assert exported['timeline'] == projected['timeline']
    for private in ('651930410', 'private-message', 'private-reply', 'sk-private', raw['letter_id']):
        assert private.encode() not in encoded


def test_reply_tolerance_receipts_survive_log_ring_and_zip_without_drafts(monkeypatch, capsys):
    from collections import deque
    import local_server
    from runtime.diagnostics.support_bundle import project_chat_task
    fields = dict(decision_defaulted_fields=['initiative', 'private-message', ['private-token']],
                  decision_warning_codes=['REPEATED_REPLY', 'private-secret'],
                  decision_dropped_media='UNREQUESTED_SPEECH',
                  decision_dropped_controls='CONTROL_SHAPE_INVALID', quality_status='accepted_with_warnings',
                  quality_violation_codes=['FOCUS_REVIEW_UNAVAILABLE', 'AUTONOMY_REVIEW_UNAVAILABLE', 'private-secret'],
                  text='private-draft', evidence='private-quote', token='private-token')
    monkeypatch.setattr(local_server, '_RUNTIME_DIAGNOSTIC_EVENTS', deque(maxlen=160))
    monkeypatch.setattr(local_server, '_RUNTIME_REQUEST_EVENTS', deque(maxlen=40))
    local_server._safe_log('personal_chat_decision_normalized', channel='qq', **fields)
    capsys.readouterr()
    source = _source()
    source['runtime_tail'] = list(local_server.runtime_diagnostic_event_snapshot())
    source['tasks']['items'][0].update(channel='qq', **fields)
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        raw = archive.read('runtime-tail.jsonl') + archive.read('tasks.json')
        tail = json.loads(archive.read('runtime-tail.jsonl'))
        task = json.loads(archive.read('tasks.json'))['items'][0]
    for row in (tail, task):
        assert row['decision_defaulted_fields'] == ['initiative']
        assert row['decision_warning_codes'] == ['REPEATED_REPLY']
        assert row['decision_dropped_media'] == 'UNREQUESTED_SPEECH'
        assert row['decision_dropped_controls'] == 'CONTROL_SHAPE_INVALID'
        assert row['quality_violation_codes'] == ['FOCUS_REVIEW_UNAVAILABLE', 'AUTONOMY_REVIEW_UNAVAILABLE']
    assert b'private-' not in raw
    assert project_chat_task(project_chat_task(task)) == project_chat_task(task)
    assert project_chat_task({'decision_dropped_controls': ['private-token']}) == {}


def test_chat_order_evidence_ignores_malformed_and_unrelated_identifiers():
    from runtime.diagnostics.support_bundle import project_chat_task
    raw = {'channel': 'qq', 'letter_id': '651930410', 'turn_ref': 'private-secret',
           'generation_attempts': True, 'life_received_at': 'private-message',
           'user_sent_at': '2026-09-26T14:38:00',  # no timezone: do not guess
           'private_world_occurred_at': '2200-01-01T00:00:00Z',
           'timeline': {'platform_sent_at': 'private', 'private-secret': '2026-09-26T00:00:00Z'}}
    assert project_chat_task(raw) == {'channel': 'qq'}


def test_chat_voice_route_reason_survives_export_without_user_content():
    from runtime.diagnostics.support_bundle import project_chat_task
    raw = dict(channel='qq', voice_ready=False, delivery_basis='PROVIDER_UNAVAILABLE',
               requested_format='text', delivered_format='text', content='private message',
               text_reason='private model annotation', key='sk-private')
    projected = project_chat_task(raw)
    assert projected == dict(channel='qq', voice_ready=False, delivery_basis='PROVIDER_UNAVAILABLE',
                             requested_format='text', delivered_format='text')
    assert project_chat_task(projected) == projected
    source = _source()
    source['tasks']['items'][0].update(raw)
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        item = json.loads(archive.read('tasks.json'))['items'][0]
    assert item['delivery_basis'] == 'PROVIDER_UNAVAILABLE' and item['voice_ready'] is False
    assert 'private' not in json.dumps(item)


def test_voice_route_projection_ignores_arbitrary_annotations_and_false_boolean_types():
    from runtime.diagnostics.support_bundle import project_chat_task
    assert project_chat_task(dict(channel='qq', voice_ready=1, delivery_basis='private reason',
        requested_format='private', delivered_format='private')) == {'channel': 'qq'}
    assert project_chat_task(dict(voice_ready=True, delivery_basis='QQ_DEFAULT_VOICE')) == {}


def test_memory_install_diagnostics_only_keep_safe_stage_and_counts():
    source = _source()
    source["health"]["checks"]["memory_install"] = {
        "state": "repair", "phase": "runtime", "source": "offline",
        "reason_code": "MEM0_RUNTIME_HASH_MISMATCH", "downloaded_bytes": 100,
        "total_bytes": 200, "remaining_bytes": 100,
        "current_file": "C:/Users/private-secret", "stderr": "private-secret",
        "install_locations": ["private-secret"],
    }
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        raw = archive.read("health.json")
    actual = json.loads(raw)["checks"]["memory_install"]
    assert actual == {"state": "repair", "phase": "runtime", "source": "offline",
        "error_code": "MEM0_RUNTIME_HASH_MISMATCH", "downloaded_bytes": 100,
        "total_bytes": 200, "remaining_bytes": 100}
    assert b"private-secret" not in raw


def test_lipsync_context_strictly_projects_labels_and_booleans():
    source = _source()
    source['media_provider_tail'] = [
        {'provider': 'latentsync', 'phase': 'inference', 'missing_component': 'audio',
         'inputs': {'audio': {'exists': False, 'readable': False, 'path': 'private-secret'},
                    'video': {'exists': True, 'readable': 'private-secret'},
                    'private-secret': {'exists': True}}},
        {'provider': 'latentsync', 'phase': ['private-secret'], 'missing_component': 'private-secret',
         'inputs': {'audio': {'exists': 1, 'readable': 'true'}}},
    ]
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        raw = archive.read('media-provider-tail.jsonl')
    records = [json.loads(line) for line in raw.splitlines()]
    assert records[0] == {'provider': 'latentsync', 'phase': 'inference', 'missing_component': 'audio',
                          'inputs': {'audio': {'exists': False, 'readable': False}, 'video': {'exists': True}}}
    assert records[1] == {'provider': 'latentsync'}
    assert b'private-secret' not in raw


def test_route_diagnostics_keep_switches_and_request_facts_without_text():
    source = _source()
    task = source['tasks']['items'][0]
    task.update(video_reply_enabled=True, reply_video_enabled=False,
        reply_routes={'voice_reply':True, 'secret':'private-secret'},
        reply_route_videos={'voice_reply':False, 'singing_video':'private-secret'},
        explicit_requests=['explicit_video_reply_request','private-secret'],
        route_reason='explicit_media_requested', request_disposition='fulfill')
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        raw = archive.read('tasks.json')
    item = json.loads(raw)['items'][0]
    assert item['reply_video_enabled'] is False
    assert item['reply_route_videos'] == {'voice_reply':False}
    assert item['explicit_requests'] == ['explicit_video_reply_request']
    assert item['route_reason'] == 'explicit_media_requested'
    assert b'private-secret' not in raw and b'private reply' not in raw
    task.update(route_reason=['private-secret'], request_disposition={'private-secret':1})
    build_diagnostic_bundle(source)


def test_media_provider_tail_projects_failure_chain_without_private_diagnostics():
    source = _source()
    source['media_provider_tail'] = [
        {'provider': 'latentsync', 'error_code': 'LATENTSYNC_FAILED',
         'diagnostic': 'returncode=unknown;stderr_category=process_timeout', 'key': 'sk-private'},
        {'error_code': 'MUSIC_REPLY_NORMAL_VIDEO_FAILED', 'timestamp': 1788793193,
         'diagnostic': 'returncode=unavailable; stderr=process timeout; exception_types=ReplyMediaError>LatentSyncReplyError>TimeoutExpired; exception_codes=LATENTSYNC_FAILED>LATENTSYNC_FAILED'},
        {'error_code': 'MEDIA_JOB_FAILED', 'diagnostic': json.dumps({
            'stage': 'render', 'candidate_code': 'MUSIC_REPLY_NORMAL_VIDEO_FAILED',
            'exception_type': 'MusicReplyError', 'private': 'C:/Users/private sk-secret'})},
        {'error_code': 'MEDIA_JOB_FAILED', 'diagnostic': 'private reply C:/Users/private sk-secret'},
    ]
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        raw = archive.read('media-provider-tail.jsonl')
        records = [json.loads(line) for line in raw.splitlines()]
    assert records[0]['stderr_category'] == 'process_timeout'
    assert records[1]['exception_types'][-1] == 'TimeoutExpired'
    assert records[1]['exception_codes'] == ['LATENTSYNC_FAILED', 'LATENTSYNC_FAILED']
    assert records[2]['stage'] == 'render'
    assert records[2]['candidate_code'] == 'MUSIC_REPLY_NORMAL_VIDEO_FAILED'
    assert records[3] == {'error_code': 'MEDIA_JOB_FAILED'}
    assert not any(secret in raw for secret in (b'sk-', b'C:/Users', b'private reply'))


@pytest.mark.parametrize('code', ['TTS_UNAVAILABLE', 'LATENTSYNC_INPUT_UNAVAILABLE', 'REPLY_VIDEO_ENCODE_FAILED'])
def test_media_failure_local_log_survives_bundle_export(tmp_path, code):
    from runtime.media.music_reply import _provider_exception_failure, ReplyMediaError
    from original_client_server import _media_provider_tail

    _provider_exception_failure('MUSIC_REPLY_NORMAL_VIDEO_FAILED', ReplyMediaError(code),
                                {'OLIVIA_LOCAL_DATA_ROOT': str(tmp_path)})
    source = _source()
    source['media_provider_tail'] = _media_provider_tail(tmp_path)
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        records = [json.loads(line) for line in archive.read('media-provider-tail.jsonl').splitlines()]
    assert records[0]['exception_codes'] == [code]


def test_media_exception_codes_reject_private_text_and_bound_chain():
    source = _source()
    source['media_provider_tail'] = [{'error_code': 'MEDIA_JOB_FAILED',
        'diagnostic': 'exception_codes=C:/Users/private.wav>sk-secret>private reply>' + '> '.join(['TTS_UNAVAILABLE'] * 20)}]
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        raw = archive.read('media-provider-tail.jsonl')
        record = json.loads(raw)
    assert record['exception_codes'] == ['TTS_UNAVAILABLE'] * 5
    assert not any(value in raw for value in (b'C:/Users', b'sk-secret', b'private reply'))


@pytest.mark.parametrize(('stderr', 'category'), [
    ('CUDA out of memory', 'cuda_out_of_memory'),
    ('ModuleNotFoundError: No module named secret_module', 'python_module_missing'),
    ('DLL load failed', 'runtime_dependency_missing'),
    ('FileNotFoundError: C:/Users/private.wav', 'configured_path_missing'),
])
def test_tts_worker_failure_export_contains_only_category(tmp_path, stderr, category):
    from tts.delivery import _record_worker_failure
    from original_client_server import _media_provider_tail

    _record_worker_failure({'OLIVIA_LOCAL_DATA_ROOT': str(tmp_path)},
                           'TTS_EXTERNAL_PROCESS_FAILED', returncode=1,
                           stderr=(stderr + ' private letter sk-secret').encode())
    source = _source()
    source['media_provider_tail'] = _media_provider_tail(tmp_path)
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        raw = archive.read('media-provider-tail.jsonl')
    record = json.loads(raw)
    assert record['stderr_category'] == category
    assert record['returncode'] == 1
    local_log = (tmp_path / 'logs/media-provider.jsonl').read_bytes()
    assert all(secret not in raw + local_log for secret in
               (b'C:/Users', b'secret_module', b'private letter', b'sk-secret'))


def test_tts_worker_status_survives_local_log_and_bundle_projection(tmp_path):
    from tts.delivery import _record_worker_failure
    from original_client_server import _media_provider_tail
    status = tmp_path / 'status.json'
    status.write_text(json.dumps({'phase': 'model_load', 'error_type': 'ModuleNotFoundError',
        'error_code': 'BREEZE_MODULE_MISSING', 'private': 'private letter sk-secret'}))
    _record_worker_failure({'OLIVIA_LOCAL_DATA_ROOT': str(tmp_path)},
        'TTS_EXTERNAL_PROCESS_FAILED', returncode=2, status_path=status)
    status.unlink()
    source = _source()
    source['media_provider_tail'] = list(_media_provider_tail(tmp_path))
    source['media_provider_tail'].append({'error_code': 'TTS_EXTERNAL_PROCESS_FAILED',
        'diagnostic': json.dumps({'worker': {'phase': 'private-path', 'error_type': 'sk-secret',
                                            'error_code': 'PRIVATE_LETTER'}})})
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        raw = archive.read('media-provider-tail.jsonl')
    records = [json.loads(line) for line in raw.splitlines()]
    assert records[0]['worker'] == {'phase': 'model_load', 'error_type': 'ModuleNotFoundError',
                                   'error_code': 'BREEZE_MODULE_MISSING'}
    assert records[0]['returncode'] == 2
    assert 'worker' not in records[1]
    assert all(secret not in raw for secret in (b'private', b'sk-secret', b'PRIVATE_LETTER'))


@pytest.mark.parametrize('mode', ['musical_video', 'spoken_video', 'voice_reply', 'singing_video', 'voice_song_video'])
def test_media_task_diagnostic_retains_exact_video_route(mode):
    source = _source()
    source['tasks']['items'][0]['reply_mode'] = mode
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        assert json.loads(archive.read('tasks.json'))['items'][0]['reply_mode'] == mode


@pytest.mark.parametrize("diagnostic", [
    "BREEZE_PIP_DISK_FULL", "BREEZE_PIP_MISSING_PIP", "BREEZE_PIP_UNSUPPORTED_WHEEL",
    "BREEZE_PIP_HASH_MISMATCH", "BREEZE_PIP_WHEEL_UNAVAILABLE", "BREEZE_PIP_ACCESS_DENIED",
    "BREEZE_PIP_TIMEOUT", "BREEZE_PIP_FAILED", "PRIVATE_KEY_SHOULD_NOT_LEAK",
])
def test_video_install_diagnostic_is_an_exact_allowlist(diagnostic):
    source = _source()
    source["health"]["checks"]["video_ordinary"] = {
        "state": "failed", "diagnostic_code": diagnostic,
    }
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        entry = json.loads(archive.read("health.json"))["checks"]["video_ordinary"]
        if diagnostic.startswith("BREEZE_PIP_"):
            assert entry["diagnostic_code"] == diagnostic
        else:
            assert "diagnostic_code" not in entry
            assert diagnostic.encode() not in archive.read("health.json")


def _contents(bundle: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_install_failure_details_survive_zip_without_raw_exception():
    source = _source()
    source['health']['checks']['video_ordinary'] = {'state': 'failed', 'failure_details': {
        'stage': 'download', 'source': 'official', 'file_id': 'weights', 'kind': 'http',
        'http_status': 403, 'message': 'private token and path', 'url': 'https://private'}}
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        raw = archive.read('health.json')
        details = json.loads(raw)['checks']['video_ordinary']['failure_details']
        assert details['http_status'] == 403 and details['file_id'] == 'weights'
        assert b'private' not in raw


def test_tts_install_component_survives_support_bundle():
    source = _source()
    source['health']['checks']['video_ordinary'] = {'state': 'failed', 'failure_details': {
        'stage': 'activate', 'component': 'voice_reference', 'kind': 'file_missing'}}
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        details = json.loads(archive.read('health.json'))['checks']['video_ordinary']['failure_details']
    assert details == {'stage': 'activate', 'component': 'voice_reference', 'kind': 'file_missing'}


def test_memory_initialization_stage_survives_support_bundle():
    source = _source()
    code = 'MEM0_INIT_BACKEND_VECTOR_STORE_VALUE'
    source['health']['checks']['memory'] = {'state': 'unavailable', 'error_code': code}
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        assert json.loads(archive.read('health.json'))['checks']['memory']['error_code'] == code


@pytest.mark.parametrize("bad", [-1, True, 10**15 + 1, "private-path"])
def test_video_progress_rejects_non_count_values(bad):
    source = _source()
    source["health"]["checks"]["video_runtime"] = {"state": "checking", "checked_bytes": bad}
    with pytest.raises(DiagnosticBundleError):
        build_diagnostic_bundle(source)


def test_running_version_only_accepts_version_tokens():
    source = _source()
    source["summary"]["running_version"] = "private-token"
    with pytest.raises(DiagnosticBundleError):
        build_diagnostic_bundle(source)


def test_memory_worker_counters_are_exported_without_private_fields():
    source = _source()
    source['health']['checks']['memory_worker'] = {
        'state': 'degraded', 'pending_count': 2, 'attempt_count': 7,
        'terminal_count': 1, 'worker_running': True,
        'pending_error_counts': {'MEM0_EXTRACTION_RESPONSE_INVALID': 2}, 'user_id': 'private-id',
    }
    result = json.loads(_contents(build_diagnostic_bundle(source))['health.json'])
    assert result['checks']['memory_worker'] == {
        'state': 'degraded', 'pending_count': 2, 'attempt_count': 7,
        'terminal_count': 1, 'worker_running': True,
        'pending_error_counts': {'MEM0_EXTRACTION_RESPONSE_INVALID': 2},
    }


def test_bundle_has_only_fixed_deterministic_members_and_safe_projection() -> None:
    first = build_diagnostic_bundle(_source())
    assert first == build_diagnostic_bundle(_source())

    contents = _contents(first)
    assert list(contents) == [
        "manifest.json",
        "summary.json",
        "health.json",
        "install.json",
        "tasks.json",
        "launcher-tail.jsonl",
        "runtime-tail.jsonl",
        "media-provider-tail.jsonl",
    ]
    joined = b"\n".join(contents.values()).decode("utf-8")
    for forbidden in (
        "real_id",
        "user-123",
        "C:\\Users\\pc",
        "private reply",
        "private.example",
        "never include this",
        "private-real-id",
    ):
        assert forbidden not in joined
    assert json.loads(contents["summary.json"]) == {
        "architecture": "AMD64",
        "contract_version": "2.0",
        "os_name": "Windows",
        "os_release": "11",
        "python_version": "3.12.4",
        "status": "available",
    }
    assert json.loads(contents["install.json"]) == {
        "key_configured": True,
        "setup_completed": True,
        "status": "available",
    }
    assert json.loads(contents["tasks.json"]) == {
        "items": [
            {
                "error_code": "LLM_TIMEOUT",
                "index": 1,
                "media_status": "not_requested",
                "reply_mode": "text_letter",
                "retryable": True,
                "stage": "failed",
                "elapsed_bucket": "5m_15m",
                "status": "failed",
            }
        ],
        "pending": 1,
        "status": "active",
    }
    assert contents["launcher-tail.jsonl"] == b'{"attempt":1,"event":"backend_ready"}\n'
    assert contents["runtime-tail.jsonl"] == b'{"event":"letter_completed","reply_mode":"text"}\n'


def test_bundle_accepts_normal_windows_launcher_exit_codes_and_missing_code() -> None:
    source = _source()
    source["launcher_tail"] = [
        {"event": "backend_unavailable", "exit_code": None},
        {"event": "client_exit", "exit_code": 0x0E000003},
        {"event": "client_exit", "exit_code": 0xC0000005},
        {"event": "client_exit", "exit_code": -1073741819},
    ]

    contents = _contents(build_diagnostic_bundle(source))
    assert contents["launcher-tail.jsonl"] == (
        b'{"event":"backend_unavailable"}\n'
        b'{"event":"client_exit","exit_code":234881027}\n'
        b'{"event":"client_exit","exit_code":3221225477}\n'
        b'{"event":"client_exit","exit_code":-1073741819}\n'
    )


@pytest.mark.parametrize(
    "source",
    (
        {},
        {"summary": {"status": "available"}},
        {
            **_source(),
            "health": {
                "status": "available",
                "checks": {"memory": {"state": "https://bad"}},
            },
        },
        {**_source(), "launcher_tail": [{"event": "private reply"}]},
        {**_source(), "runtime_tail": [{"event": "private reply"}]},
    ),
)
def test_bundle_rejects_invalid_input_without_emitting_partial_archive(
    source: dict[str, object],
) -> None:
    with pytest.raises(DiagnosticBundleError, match="DIAGNOSTIC_BUNDLE_INPUT_INVALID"):
        build_diagnostic_bundle(source)


@pytest.mark.parametrize("counts", [
    {"private/path-or-key": 1}, {"X" * 97: 1},
    {"MEM0_WRITE_FAILED": True}, {"MEM0_WRITE_FAILED": -1},
    {"MEM0_WRITE_FAILED": 1_000_000_001},
    {f"ERROR_{i}": 1 for i in range(17)},
])
def test_pending_error_counts_reject_private_or_unbounded_values(counts):
    source = _source()
    source["health"]["checks"]["memory_worker"] = {"state": "degraded", "pending_error_counts": counts}
    with pytest.raises(DiagnosticBundleError):
        build_diagnostic_bundle(source)


def test_launcher_tail_keeps_startup_timings_but_not_paths():
    from runtime.diagnostics.support_bundle import _project_tail_record
    record = _project_tail_record({"event": "backend_phase", "phase": "startup_hook:_start_reply_tasks",
                                   "elapsed_seconds": 8.4217, "timestamp": 1790787742.2}, runtime=False)
    assert record == {"event": "backend_phase", "phase": "startup_hook:_start_reply_tasks", "elapsed_seconds": 8.422}
    record = _project_tail_record({"event": "backend_phase", "phase": "C:/Users/someone", "elapsed_seconds": -1,
                                   "preparation_seconds": 16.5}, runtime=False)
    assert record == {"event": "backend_phase", "preparation_seconds": 16.5}


def test_startup_animation_does_not_cover_other_windows():
    from pathlib import Path
    script = (Path(__file__).resolve().parents[2] / "installer" / "startup_animation.ps1").read_text(encoding="utf-8")
    assert "$window.Topmost = $false" in script and "$window.Topmost = $true" not in script
