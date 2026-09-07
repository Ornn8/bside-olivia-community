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
    assert records[2]['stage'] == 'render'
    assert records[2]['candidate_code'] == 'MUSIC_REPLY_NORMAL_VIDEO_FAILED'
    assert records[3] == {'error_code': 'MEDIA_JOB_FAILED'}
    assert not any(secret in raw for secret in (b'sk-', b'C:/Users', b'private reply'))


@pytest.mark.parametrize('mode', ['musical_video', 'spoken_video'])
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
