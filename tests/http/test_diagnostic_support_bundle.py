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


def _contents(bundle: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_memory_worker_counters_are_exported_without_private_fields():
    source = _source()
    source['health']['checks']['memory_worker'] = {
        'state': 'degraded', 'pending_count': 2, 'attempt_count': 7,
        'terminal_count': 1, 'worker_running': True, 'user_id': 'private-id',
    }
    result = json.loads(_contents(build_diagnostic_bundle(source))['health.json'])
    assert result['checks']['memory_worker'] == {
        'state': 'degraded', 'pending_count': 2, 'attempt_count': 7,
        'terminal_count': 1, 'worker_running': True,
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
