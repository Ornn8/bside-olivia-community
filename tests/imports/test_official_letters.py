from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from jsonschema import Draft202012Validator, FormatChecker

from conversation_memory_port import (
    ConversationMemoryStatus,
    MemoryWriteResult,
    MemoryWriteStatus,
    NullConversationMemoryPort,
)
from llm_gateway import GatewayResponse
from memory_port import LegacyImportResult, NullMemoryPort
from private_world_ledger import SQLitePrivateWorldLedger
from private_world_port import PrivateWorldSnapshot
from private_world_service import PrivateWorldCommandService
from runtime.imports.official_letters import (
    build_legacy_import_payload,
    collect_official_text_replies,
)


def _allow_official_history_preflight(monkeypatch, local_server) -> None:
    monkeypatch.setattr(
        local_server, "_official_history_memory_available", lambda: True
    )
    monkeypatch.setattr(
        local_server, "_official_history_private_world_available", lambda: True
    )
    monkeypatch.setattr(
        local_server,
        "LLM_CONFIG",
        local_server.GatewayConfig(provider="mock", feature_enabled=True),
    )


class _ExistingPrivateWorld:
    def snapshot(self) -> PrivateWorldSnapshot:
        return PrivateWorldSnapshot(version=2, trust=1)


class _ExistingHistoryAudit:
    @staticmethod
    def lookup_command(_command_id: str) -> object:
        return object()


def test_official_import_progress_response_matches_public_schema(monkeypatch) -> None:
    import local_server

    monkeypatch.setattr(
        local_server,
        "_official_import_progress",
        {
            "status": "RUNNING",
            "stage": "reading",
            "total": 6,
            "processed": 2,
            "imported": 0,
            "skipped": 0,
            "last_updated_at": "2026-08-29T13:30:00+00:00",
            "retryable": False,
        },
    )
    response = asyncio.run(
        local_server.route("GET", "/toy/letter/legacy/official-import", {}, {})
    )
    schema = json.loads(
        (Path("contracts") / "official_history_import_progress.schema.json").read_text(
            encoding="utf-8"
        )
    )

    Draft202012Validator(schema, format_checker=FormatChecker()).validate(response)


def test_official_import_preflight_is_a_versioned_public_http_contract(
    monkeypatch,
) -> None:
    import local_server

    class EmptyPrivateWorld:
        @staticmethod
        def snapshot() -> PrivateWorldSnapshot:
            return PrivateWorldSnapshot()

    _allow_official_history_preflight(monkeypatch, local_server)
    monkeypatch.setattr(local_server, "private_world_port", EmptyPrivateWorld())
    schema = json.loads(
        (Path("contracts") / "official_history_import_preflight.schema.json").read_text(
            encoding="utf-8"
        )
    )

    async def scenario() -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app, access_log=None)) as client:
            ready_response = await client.get(
                "/toy/letter/legacy/official-import?preflight=1"
            )
            ready_payload = await ready_response.json()
            assert ready_response.status == 200
            Draft202012Validator(schema).validate(ready_payload)

            monkeypatch.setattr(
                local_server,
                "LLM_CONFIG",
                local_server.GatewayConfig(provider="none", feature_enabled=False),
            )
            unavailable_response = await client.get(
                "/toy/letter/legacy/official-import?preflight=1"
            )
            assert unavailable_response.status == 503
            assert await unavailable_response.json() == {
                "code": 503,
                "message": "OFFICIAL_HISTORY_LLM_UNAVAILABLE",
                "data": {
                    "status": "UNAVAILABLE",
                    "error_code": "OFFICIAL_HISTORY_LLM_UNAVAILABLE",
                    "retryable": True,
                },
            }

    asyncio.run(scenario())


def test_history_skip_requires_current_first_person_semantics(monkeypatch) -> None:
    import local_server

    class Archive:
        enabled = True

        @staticmethod
        def list_legacy():
            return [
                {
                    "source_record_id": "official:old",
                    "metadata": {"official_history_publish_status": "completed_v1"},
                },
                {
                    "source_record_id": "official:first-person-v1",
                    "metadata": {
                        "official_history_publish_status": "completed_v1",
                        "official_history_memory_semantics": "linli_first_person_v1",
                    },
                },
                {
                    "source_record_id": "official:current",
                    "metadata": {
                        "official_history_publish_status": "completed_v1",
                        "official_history_memory_semantics": "actor_split_first_person_v2",
                    },
                },
            ]

    monkeypatch.setattr(local_server, "memory_adapter", Archive())

    assert local_server._existing_legacy_source_record_ids() == frozenset(
        {"official:current"}
    )


def test_missing_legacy_table_uses_empty_bootstrap_store(monkeypatch) -> None:
    import local_server

    class Archive:
        enabled = True

        @staticmethod
        def list_legacy():
            raise sqlite3.OperationalError("no such table: legacy_letters")

    monkeypatch.setattr(local_server, "memory_adapter", Archive())
    monkeypatch.setattr(local_server.store, "legacy_letters", [])

    assert local_server._legacy_letter_collection(strict=True) == []


def test_legacy_archive_is_reopened_after_process_restart(monkeypatch) -> None:
    import local_server

    imported = {"letter_id": "official-history", "reply_text": "historical reply"}

    class Archive:
        enabled = True

        @staticmethod
        def list_legacy():
            return [imported]

    monkeypatch.setattr(local_server, "memory_adapter", NullMemoryPort())
    monkeypatch.setattr(local_server, "_legacy_import_adapter", lambda: Archive())

    assert local_server._legacy_letter_collection(strict=True) == [imported]
    assert local_server._existing_legacy_source_record_ids() == frozenset()


def test_reopened_legacy_archive_is_merged_with_loaded_bootstrap_records(
    monkeypatch,
) -> None:
    import local_server

    archived = {
        "letter_id": "official-history",
        "source_record_id": "official:history",
        "reply_text": "historical reply",
    }
    loaded = {
        "letter_id": "bootstrap-history",
        "source_record_id": "bootstrap:history",
        "reply_text": "bootstrap reply",
    }

    class Archive:
        enabled = True

        @staticmethod
        def list_legacy():
            return [archived]

    monkeypatch.setattr(local_server, "memory_adapter", NullMemoryPort())
    monkeypatch.setattr(local_server.store, "legacy_letters", [loaded])
    monkeypatch.setattr(local_server, "_legacy_import_adapter", lambda: Archive())

    assert local_server._legacy_letter_collection(strict=True) == [archived, loaded]


def test_other_legacy_sqlite_errors_fail_closed(monkeypatch) -> None:
    import local_server

    class Archive:
        enabled = True

        @staticmethod
        def list_legacy():
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(local_server, "memory_adapter", Archive())

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        local_server._legacy_letter_collection(strict=True)
    assert local_server._existing_legacy_source_record_ids() is None


def test_official_text_replies_become_read_only_legacy_pairs() -> None:
    payload = build_legacy_import_payload(
        [
            {
                "letter_id": "letter-text",
                "created_at": 1710000000,
                "replied_at": 1710000100,
                "content": "用户写下的旧信",
                "reply_content": "林离过去的文字回信",
                "reply_video_url": "",
            },
            {
                "letter_id": "letter-video",
                "created_at": 1710000200,
                "content": "只有视频回信的旧信",
                "reply_text": "",
                "reply_video_url": "https://example.invalid/video.mp4",
            },
        ],
        account_id="official-user-1",
    )

    assert payload == {
        "mode": "read_only",
        "account_id": "official-user-1",
        "letters": [
            {
                "source_record_id": "official:official-user-1:letter-text",
                "source": "official-olivia",
                "occurred_at": 1710000000,
                "content": "用户来信：用户写下的旧信\n林离回信：林离过去的文字回信",
                "metadata": {
                    "user_content": "用户写下的旧信",
                    "reply_text": "林离过去的文字回信",
                    "replied_at": 1710000100,
                    "import_kind": "official_text_reply",
                    "official_account_id": "official-user-1",
                },
            }
        ],
    }
    assert "reply_video_url" not in repr(payload)


def test_official_log_credentials_are_used_but_never_enter_import_payload(
    tmp_path,
) -> None:
    log_path = tmp_path / "Olivia.log"
    log_path.write_text(
        'network_request {"request.url":"/signIn",'
        '"x-token":"secret-token","x-uid":"200717",'
        '"x-pkg_version":"0.0.9.627"}',
        encoding="utf-8",
    )
    calls: list[str] = []

    def request_json(path: str, headers: dict[str, str]) -> dict:
        assert headers["x-token"] == "secret-token"
        calls.append(path)
        if path.startswith("/letter/list"):
            return {
                "code": 0,
                "data": {
                    "list": [{"letter_id": "letter-1", "created_at": 1710000000}],
                    "has_more": False,
                },
            }
        return {
            "code": 0,
            "data": {
                "letter_id": "letter-1",
                "created_at": 1710000000,
                "replied_at": 1710000100,
                "content": "旧信正文",
                "reply_content": "旧文字回信",
                "reply_video_url": "",
            },
        }

    payload = collect_official_text_replies(log_path, request_json=request_json)

    assert calls == [
        "/letter/list?cursor=0&page_size=50",
        "/letter/detail?letter_id=letter-1",
    ]
    assert payload["letters"][0]["metadata"]["reply_text"] == "旧文字回信"
    assert payload["account_id"] == "200717"
    assert "secret-token" not in repr(payload)
    assert "200717" in payload["letters"][0]["source_record_id"]


def test_official_log_skips_newer_sign_in_entry_with_empty_uid(tmp_path) -> None:
    log_path = tmp_path / "Olivia.log"
    log_path.write_text(
        '\n'.join(
            [
                'network_request {"x-token":"usable-token","x-uid":"200717"}',
                'network_request {"request.url":"/signIn",'
                '"x-token":"newer-token","x-uid":""}',
            ]
        ),
        encoding="utf-8",
    )
    observed: dict[str, str] = {}

    def request_json(path: str, headers: dict[str, str]) -> dict:
        observed.update(headers)
        if path.startswith("/letter/list"):
            return {"code": 0, "data": {"list": [], "has_more": False}}
        raise AssertionError(path)

    payload = collect_official_text_replies(log_path, request_json=request_json)

    assert payload == {"mode": "read_only", "account_id": "200717", "letters": []}
    assert observed["x-token"] == "usable-token"
    assert observed["x-uid"] == "200717"


def test_official_import_follows_mailbox_cursor_pages(tmp_path) -> None:
    log_path = tmp_path / "Olivia.log"
    log_path.write_text(
        'network_request {"request.url":"/signIn",'
        '"x-token":"secret-token","x-uid":"200717"}',
        encoding="utf-8",
    )
    list_paths: list[str] = []

    def request_json(path: str, _headers: dict[str, str]) -> dict:
        if path.startswith("/letter/list"):
            list_paths.append(path)
            if "cursor=0" in path:
                return {
                    "code": 0,
                    "data": {
                        "list": [{"letter_id": "letter-1", "created_at": 1710000000}],
                        "has_more": True,
                        "next_cursor": 50,
                    },
                }
            return {
                "code": 0,
                "data": {
                    "list": [{"letter_id": "letter-2", "created_at": 1710000200}],
                    "has_more": False,
                },
            }
        letter_id = path.rsplit("=", 1)[-1]
        return {
            "code": 0,
            "data": {
                "letter_id": letter_id,
                "content": f"旧信 {letter_id}",
                "reply_content": f"回信 {letter_id}",
            },
        }

    payload = collect_official_text_replies(log_path, request_json=request_json)

    assert list_paths == [
        "/letter/list?cursor=0&page_size=50",
        "/letter/list?cursor=50&page_size=50",
    ]
    assert len(payload["letters"]) == 2


def test_official_import_reports_listing_and_detail_progress(tmp_path) -> None:
    log_path = tmp_path / "Olivia.log"
    log_path.write_text(
        'network_request {"x-token":"secret-token","x-uid":"200717"}',
        encoding="utf-8",
    )
    progress: list[dict[str, object]] = []

    def request_json(path: str, _headers: dict[str, str]) -> dict:
        if path.startswith("/letter/list"):
            return {
                "code": 0,
                "data": {
                    "list": [{"letter_id": "letter-1"}, {"letter_id": "letter-2"}],
                    "has_more": False,
                },
            }
        return {
            "code": 0,
            "data": {
                "letter_id": path.rsplit("=", 1)[-1],
                "created_at": 1710000000,
                "content": "old letter",
                "reply_content": "old reply",
            },
        }

    collect_official_text_replies(
        log_path,
        request_json=request_json,
        on_progress=lambda value: progress.append(dict(value)),
    )

    assert progress == [
        {"stage": "listing", "total": 0, "processed": 0},
        {"stage": "listing", "total": 2, "processed": 0},
        {"stage": "reading", "total": 2, "processed": 0},
        {"stage": "reading", "total": 2, "processed": 1},
        {"stage": "reading", "total": 2, "processed": 2},
    ]


def test_official_import_rejects_text_reply_without_a_valid_timestamp(tmp_path) -> None:
    log_path = tmp_path / "Olivia.log"
    log_path.write_text(
        'network_request {"x-token":"secret-token","x-uid":"200717"}',
        encoding="utf-8",
    )

    def request_json(path: str, _headers: dict[str, str]) -> dict:
        if path.startswith("/letter/list"):
            return {
                "code": 0,
                "data": {"list": [{"letter_id": "letter-1"}], "has_more": False},
            }
        return {
            "code": 0,
            "data": {
                "content": "旧信",
                "reply_content": "旧文字回信",
            },
        }

    with pytest.raises(ValueError, match="OFFICIAL_LETTER_TIMESTAMP_INVALID"):
        collect_official_text_replies(log_path, request_json=request_json)


def test_official_import_fails_closed_when_any_letter_detail_is_unavailable(
    tmp_path,
) -> None:
    log_path = tmp_path / "Olivia.log"
    log_path.write_text(
        'network_request {"x-token":"secret-token","x-uid":"200717"}',
        encoding="utf-8",
    )

    def request_json(path: str, _headers: dict[str, str]) -> dict:
        if path.startswith("/letter/list"):
            return {
                "code": 0,
                "data": {
                    "list": [{"letter_id": "letter-1"}, {"letter_id": "letter-2"}],
                    "has_more": False,
                },
            }
        if path.endswith("letter-1"):
            return {"code": 0, "data": {"content": "one", "reply_text": "reply"}}
        return {"code": 503, "data": None}

    with pytest.raises(ValueError, match="OFFICIAL_LETTER_DETAIL_UNAVAILABLE"):
        collect_official_text_replies(log_path, request_json=request_json)


def test_official_import_requires_available_mem0_before_collecting_history(
    monkeypatch,
) -> None:
    import local_server

    calls: list[str] = []
    monkeypatch.setattr(
        local_server,
        "conversation_memory_adapter",
        NullConversationMemoryPort(),
    )
    monkeypatch.setattr(
        local_server,
        "collect_default_official_text_replies",
        lambda: calls.append("collect") or {},
    )
    monkeypatch.setattr(
        local_server,
        "_legacy_import_adapter",
        lambda: (_ for _ in ()).throw(AssertionError("archive must not open")),
    )

    response = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/legacy/official-import",
            {},
            {},
            companion_confirmed=True,
        )
    )

    assert response == {
        "code": 503,
        "message": "OFFICIAL_HISTORY_MEMORY_UNAVAILABLE",
        "data": {
            "status": "UNAVAILABLE",
            "error_code": "OFFICIAL_HISTORY_MEMORY_UNAVAILABLE",
            "retryable": True,
        },
    }
    assert calls == []


def test_official_import_get_exposes_observable_progress() -> None:
    import local_server

    response = asyncio.run(
        local_server.route(
            "GET",
            "/toy/letter/legacy/official-import",
            {},
            {},
        )
    )

    assert response["code"] == 0
    assert set(response["data"]) == {
        "status",
        "stage",
        "total",
        "processed",
        "imported",
        "skipped",
        "last_updated_at",
        "retryable",
    }
    assert response["data"]["stage"] in {
        "idle",
        "listing",
        "reading",
        "memory",
        "importing",
        "completed",
        "failed",
    }
    assert response["data"]["last_updated_at"].endswith("+00:00")


def test_official_import_failure_keeps_last_progress_and_allows_retry(
    monkeypatch,
) -> None:
    import local_server

    _allow_official_history_preflight(monkeypatch, local_server)

    def fail_after_first_letter(*, on_progress) -> dict[str, object]:
        on_progress({"stage": "reading", "total": 3, "processed": 1})
        raise ValueError("synthetic official endpoint failure")

    monkeypatch.setattr(
        local_server,
        "collect_default_official_text_replies",
        fail_after_first_letter,
    )

    failed = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/legacy/official-import",
            {},
            {},
            companion_confirmed=True,
        )
    )
    progress = asyncio.run(
        local_server.route(
            "GET",
            "/toy/letter/legacy/official-import",
            {},
            {},
        )
    )

    assert failed["code"] == 503
    assert progress["data"] | {"last_updated_at": "ignored"} == {
        "status": "FAILED",
        "stage": "failed",
        "total": 3,
        "processed": 1,
        "imported": 0,
        "skipped": 0,
        "last_updated_at": "ignored",
        "retryable": True,
    }


def test_official_import_does_not_publish_mailbox_when_mem0_write_fails(
    monkeypatch,
) -> None:
    import local_server

    _allow_official_history_preflight(monkeypatch, local_server)

    class ReadyMemory:
        enabled = True

        def status(self):
            return type(
                "Status",
                (),
                {"status": "available", "enabled": True, "provider": "mem0"},
            )()

    payload = {
        "mode": "read_only",
        "account_id": "200717",
        "letters": [
            {
                "source_record_id": "official:200717:letter-1",
                "source": "official-olivia",
                "occurred_at": 1710000000,
                "content": "historical pair",
                "metadata": {
                    "user_content": "historical user letter",
                    "reply_text": "historical reply",
                    "import_kind": "official_text_reply",
                    "official_account_id": "200717",
                },
            }
        ],
    }
    monkeypatch.setattr(local_server, "conversation_memory_adapter", ReadyMemory())
    monkeypatch.setattr(
        local_server,
        "collect_default_official_text_replies",
        lambda: payload,
    )
    monkeypatch.setattr(local_server, "_official_account_conflicts", lambda _body: False)
    monkeypatch.setattr(
        local_server,
        "_legacy_import_adapter",
        lambda: (_ for _ in ()).throw(AssertionError("archive must not publish")),
    )

    async def failed_migration(_body):
        return local_server.HistoricalMigrationResult(
            "partial",
            1,
            0,
            0,
            0,
            0,
            error_code="MEM0_WRITE_FAILED",
        )

    monkeypatch.setattr(local_server, "_migrate_official_history", failed_migration)

    response = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/legacy/official-import",
            {},
            {},
            companion_confirmed=True,
        )
    )

    assert response["code"] == 503
    assert response["data"] == {
        "status": "UNAVAILABLE",
        "error_code": "OFFICIAL_HISTORY_MEMORY_WRITE_FAILED",
        "retryable": True,
        "migration": {
            "status": "partial",
            "total": 1,
            "processed": 0,
            "written": 0,
            "duplicates": 0,
            "skipped": 0,
            "private_world_status": None,
            "error_code": "MEM0_WRITE_FAILED",
        },
    }


def test_official_import_requires_private_world_before_collecting_history(
    monkeypatch,
) -> None:
    import local_server

    class ReadyMemory:
        enabled = True

        def status(self):
            return type(
                "Status",
                (),
                {"status": "available", "enabled": True, "provider": "mem0"},
            )()

    calls: list[str] = []
    monkeypatch.setattr(local_server, "conversation_memory_adapter", ReadyMemory())
    monkeypatch.setattr(local_server, "private_world_command_service", None)
    monkeypatch.setattr(
        local_server,
        "collect_default_official_text_replies",
        lambda: calls.append("collect") or {},
    )

    response = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/legacy/official-import",
            {},
            {},
            companion_confirmed=True,
        )
    )

    assert response["code"] == 503
    assert response["data"] == {
        "status": "UNAVAILABLE",
        "error_code": "PRIVATE_WORLD_HISTORY_UNAVAILABLE",
        "retryable": True,
    }
    assert calls == []


def test_official_import_requires_llm_before_collecting_or_writing_history(
    monkeypatch,
) -> None:
    import local_server

    calls: list[str] = []
    _allow_official_history_preflight(monkeypatch, local_server)
    monkeypatch.setattr(
        local_server,
        "LLM_CONFIG",
        local_server.GatewayConfig(provider="none", feature_enabled=False),
    )
    monkeypatch.setattr(
        local_server,
        "collect_default_official_text_replies",
        lambda: calls.append("collect") or {},
    )
    monkeypatch.setattr(
        local_server,
        "_legacy_import_adapter",
        lambda: (_ for _ in ()).throw(AssertionError("archive must not open")),
    )

    response = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/legacy/official-import",
            {},
            {},
            companion_confirmed=True,
        )
    )
    progress = asyncio.run(
        local_server.route(
            "GET",
            "/toy/letter/legacy/official-import",
            {},
            {},
        )
    )

    assert local_server.contract.error_metadata(
        "OFFICIAL_HISTORY_LLM_UNAVAILABLE"
    ) == {"http_status": 503, "retryable": True}
    assert response == {
        "code": 503,
        "message": "OFFICIAL_HISTORY_LLM_UNAVAILABLE",
        "data": {
            "status": "UNAVAILABLE",
            "error_code": "OFFICIAL_HISTORY_LLM_UNAVAILABLE",
            "retryable": True,
        },
    }
    assert progress["data"]["processed"] == 0
    assert progress["data"]["imported"] == 0
    assert calls == []


def test_existing_private_world_without_corpus_audit_requires_llm_before_any_io(
    monkeypatch,
) -> None:
    import local_server

    class RecordingMemory:
        enabled = True

        def __init__(self) -> None:
            self.write_calls = 0

        def status(self) -> ConversationMemoryStatus:
            return ConversationMemoryStatus(
                "available", True, "mem0", "qdrant-local"
            )

        def remember_exchange(self, **kwargs: object) -> MemoryWriteResult:
            self.write_calls += 1
            return MemoryWriteResult(
                MemoryWriteStatus.WRITTEN,
                str(kwargs["source_id"]),
                ("memory.synthetic",),
            )

    class NoAuditService:
        @staticmethod
        def lookup_command(_command_id: str) -> None:
            return None

    class RecordingGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, _messages, *, request_id=None) -> GatewayResponse:
            del request_id
            self.calls += 1
            raise AssertionError("provider must not run")

    memory = RecordingMemory()
    gateway = RecordingGateway()
    collect_calls: list[str] = []
    archive_opens: list[str] = []
    official_payload = build_legacy_import_payload(
        (
            {
                "letter_id": "letter-no-audit",
                "content": "synthetic user letter",
                "reply_text": "synthetic official reply",
                "created_at": 1710000000,
                "replied_at": 1710000100,
            },
        ),
        account_id="synthetic-account",
    )

    monkeypatch.setattr(local_server, "conversation_memory_adapter", memory)
    monkeypatch.setattr(local_server, "memory_adapter", NullMemoryPort())
    monkeypatch.setattr(local_server, "private_world_port", _ExistingPrivateWorld())
    monkeypatch.setattr(
        local_server, "private_world_command_service", NoAuditService()
    )
    monkeypatch.setattr(local_server.letters_adapter, "gateway", gateway)
    monkeypatch.setattr(
        local_server,
        "LLM_CONFIG",
        local_server.GatewayConfig(provider="none", feature_enabled=False),
    )
    monkeypatch.setattr(
        local_server,
        "collect_default_official_text_replies",
        lambda: collect_calls.append("collect") or official_payload,
    )
    monkeypatch.setattr(
        local_server,
        "_legacy_import_adapter",
        lambda: archive_opens.append("open") or NullMemoryPort(),
    )
    monkeypatch.setattr(local_server.store, "legacy_letters", [])

    preflight = asyncio.run(
        local_server.route(
            "GET",
            "/toy/letter/legacy/official-import",
            {},
            {"preflight": "1"},
        )
    )
    confirmed = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/legacy/official-import",
            {},
            {},
            companion_confirmed=True,
        )
    )

    expected = {
        "code": 503,
        "message": "OFFICIAL_HISTORY_LLM_UNAVAILABLE",
        "data": {
            "status": "UNAVAILABLE",
            "error_code": "OFFICIAL_HISTORY_LLM_UNAVAILABLE",
            "retryable": True,
        },
    }
    assert preflight == expected
    assert confirmed == expected
    assert collect_calls == []
    assert memory.write_calls == 0
    assert gateway.calls == 0
    assert archive_opens == []


def test_official_import_preflight_reports_llm_before_user_confirmation(
    monkeypatch,
) -> None:
    import local_server

    _allow_official_history_preflight(monkeypatch, local_server)
    monkeypatch.setattr(
        local_server,
        "LLM_CONFIG",
        local_server.GatewayConfig(provider="none", feature_enabled=False),
    )

    response = asyncio.run(
        local_server.route(
            "GET",
            "/toy/letter/legacy/official-import",
            {},
            {"preflight": "1"},
        )
    )

    assert response == {
        "code": 503,
        "message": "OFFICIAL_HISTORY_LLM_UNAVAILABLE",
        "data": {
            "status": "UNAVAILABLE",
            "error_code": "OFFICIAL_HISTORY_LLM_UNAVAILABLE",
            "retryable": True,
        },
    }


def test_official_import_ready_preflight_does_not_call_the_provider(
    monkeypatch,
) -> None:
    import local_server

    class EmptyPrivateWorld:
        @staticmethod
        def snapshot() -> PrivateWorldSnapshot:
            return PrivateWorldSnapshot()

    _allow_official_history_preflight(monkeypatch, local_server)
    monkeypatch.setattr(local_server, "private_world_port", EmptyPrivateWorld())
    network_calls_before = local_server.letters_adapter.gateway.network_call_count

    response = asyncio.run(
        local_server.route(
            "GET",
            "/toy/letter/legacy/official-import",
            {},
            {"preflight": "1"},
        )
    )

    assert response == {
        "code": 0,
        "message": "ok",
        "data": {"status": "READY", "llm_required": True},
    }
    assert (
        local_server.letters_adapter.gateway.network_call_count
        == network_calls_before
    )


def test_official_history_is_visible_in_current_mailbox_without_unread_pollution(
    monkeypatch,
) -> None:
    import local_server

    imported = {
        "letter_id": "legacy-official-1",
        "created_at": 1710000000,
        "content": "historical user letter",
        "reply_text": "historical reply",
        "reply_video_url": "",
        "is_read": 0,
        "read_only": True,
        "metadata": {
            "import_kind": "official_text_reply",
            "official_history_publish_status": "completed_v1",
        },
    }
    monkeypatch.setattr(local_server.store, "letters", [])
    monkeypatch.setattr(
        local_server,
        "_legacy_letter_collection",
        lambda *, strict=False: [imported],
    )

    listed = asyncio.run(local_server.route("GET", "/toy/letter/list", {}, {}))
    unread = asyncio.run(
        local_server.route("GET", "/toy/letter/unread_count", {}, {})
    )
    detail = asyncio.run(
        local_server.route(
            "GET",
            "/toy/letter/detail",
            {},
            {"letter_id": "legacy-official-1"},
        )
    )

    assert listed["data"]["total"] == 1
    assert listed["data"]["list"][0]["summary"] == "historical user letter"
    assert local_server._letter_collection("current")[0]["letter_status"] == "COMPLETED"
    assert unread["data"]["unread_count"] == 0
    assert detail["data"]["content"] == "historical user letter"
    assert detail["data"]["reply_text"] == "historical reply"
    assert detail["data"]["scope"] == "legacy"
    assert detail["data"]["read_only"] is True


def test_unmarked_official_archive_from_an_older_build_stays_out_of_mailbox(
    monkeypatch,
) -> None:
    import local_server

    archived_before_memory_completed = {
        "letter_id": "legacy-official-partial",
        "created_at": 1710000000,
        "content": "historical user letter",
        "reply_text": "historical reply",
        "is_read": 0,
        "read_only": True,
        "metadata": {"import_kind": "official_text_reply"},
    }
    monkeypatch.setattr(local_server.store, "letters", [])
    monkeypatch.setattr(
        local_server,
        "_legacy_letter_collection",
        lambda *, strict=False: [archived_before_memory_completed],
    )

    listed = asyncio.run(local_server.route("GET", "/toy/letter/list", {}, {}))

    assert listed["data"]["total"] == 0


def test_official_import_persists_memory_before_publishing_read_only_mailbox(
    tmp_path,
    monkeypatch,
) -> None:
    import local_server

    monkeypatch.setattr(
        local_server,
        "LLM_CONFIG",
        local_server.GatewayConfig(provider="mock", feature_enabled=True),
    )

    class PersistingMemory:
        enabled = True

        def __init__(self) -> None:
            self.records: dict[str, dict[str, object]] = {}

        def status(self) -> ConversationMemoryStatus:
            return ConversationMemoryStatus(
                "available",
                True,
                "mem0",
                "qdrant-local",
                memory_count=len(self.records),
            )

        def remember_exchange(self, **kwargs: object) -> MemoryWriteResult:
            source_id = str(kwargs["source_id"])
            if source_id in self.records:
                return MemoryWriteResult(MemoryWriteStatus.DUPLICATE, source_id)
            self.records[source_id] = dict(kwargs)
            return MemoryWriteResult(
                MemoryWriteStatus.WRITTEN,
                source_id,
                (f"memory.{len(self.records)}",),
            )

        def list_memories(self, *, user_id: str, limit: int = 100):
            del user_id
            return tuple(self.records.values())[:limit]

    memory = PersistingMemory()
    official_payload = {
        "mode": "read_only",
        "account_id": "200717",
        "letters": [
            {
                "source_record_id": "official:200717:letter-1",
                "source": "official-olivia",
                "occurred_at": 1710000000,
                "content": "用户来信：旧信正文\n林离回信：旧文字回信",
                "metadata": {
                    "user_content": "旧信正文",
                    "reply_text": "旧文字回信",
                    "replied_at": 1710000100,
                    "import_kind": "official_text_reply",
                    "official_account_id": "200717",
                    "official_history_publish_status": "completed_v1",
                },
            }
        ],
    }
    monkeypatch.setenv("OLIVIA_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setattr(local_server, "memory_adapter", NullMemoryPort())
    monkeypatch.setattr(local_server.letters_adapter, "memory_port", NullMemoryPort())
    monkeypatch.setattr(local_server, "conversation_memory_adapter", memory)
    monkeypatch.setattr(local_server, "private_world_port", _ExistingPrivateWorld())
    monkeypatch.setattr(
        local_server, "private_world_command_service", _ExistingHistoryAudit()
    )
    monkeypatch.setattr(
        local_server,
        "collect_default_official_text_replies",
        lambda: official_payload,
        raising=False,
    )

    forged = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/legacy/import",
            official_payload,
            {},
        )
    )
    before_success = asyncio.run(
        local_server.route("GET", "/toy/letter/list", {}, {})
    )
    imported = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/legacy/official-import",
            {},
            {},
            companion_confirmed=True,
        )
    )
    progress = asyncio.run(
        local_server.route(
            "GET",
            "/toy/letter/legacy/official-import",
            {},
            {},
        )
    )
    duplicate = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/legacy/official-import",
            {},
            {},
            companion_confirmed=True,
        )
    )
    current = asyncio.run(local_server.route("GET", "/toy/letter/list", {}, {}))
    listed = asyncio.run(
        local_server.route("GET", "/toy/letter/list", {}, {"scope": "legacy"})
    )
    current_unread = asyncio.run(
        local_server.route("GET", "/toy/letter/unread_count", {}, {})
    )
    assert imported["code"] == 0, imported
    assert duplicate["code"] == 0, duplicate
    letter_id = current["data"]["list"][0]["letter_id"]
    detail = asyncio.run(
        local_server.route(
            "GET",
            "/toy/letter/detail",
            {},
            {"letter_id": letter_id},
        )
    )

    assert forged["code"] == 0
    assert forged["data"]["inserted"] == 1
    assert before_success["data"]["total"] == 0
    assert imported["data"]["inserted"] == 0
    assert imported["data"]["duplicates"] == 1
    assert duplicate["data"]["inserted"] == 0
    assert duplicate["data"]["duplicates"] == 1
    assert progress["data"]["status"] == "COMPLETED"
    assert progress["data"]["stage"] == "completed"
    assert progress["data"]["total"] == 1
    assert progress["data"]["processed"] == 1
    assert progress["data"]["imported"] == 0
    assert progress["data"]["skipped"] == 1
    assert progress["data"]["retryable"] is False
    archived = local_server._legacy_letter_collection()
    assert archived[0]["metadata"]["official_history_publish_status"] == "completed_v1"
    assert archived[0]["metadata"]["official_history_memory_semantics"] == (
        "actor_split_first_person_v2"
    )
    assert len(memory.list_memories(user_id="local-user")) == 1
    remembered = memory.list_memories(user_id="local-user")[0]
    assert remembered["user_message"] == "旧信正文"
    assert remembered["assistant_message"] == "旧文字回信"
    assert current["data"]["total"] == 1
    assert current["data"]["list"][0]["summary"] == "旧信正文"
    assert current_unread["data"]["unread_count"] == 0
    assert listed["data"]["total"] == 1
    assert listed["data"]["list"][0]["summary"] == "旧信正文"
    assert detail["data"]["content"] == "旧信正文"
    assert detail["data"]["reply_text"] == "旧文字回信"
    assert detail["data"]["reply_video_url"] == ""
    assert detail["data"]["read_only"] is True
    assert detail["data"]["scope"] == "legacy"


def test_official_duplicate_does_not_retry_a_previously_skipped_memory(
    tmp_path,
    monkeypatch,
) -> None:
    import local_server

    monkeypatch.setattr(
        local_server,
        "LLM_CONFIG",
        local_server.GatewayConfig(provider="mock", feature_enabled=True),
    )

    class SkipThenWriteMemory:
        enabled = True

        def __init__(self) -> None:
            self.calls = 0

        def status(self) -> ConversationMemoryStatus:
            return ConversationMemoryStatus(
                "available",
                True,
                "mem0",
                "qdrant-local",
                memory_count=0,
            )

        def remember_exchange(self, **kwargs: object) -> MemoryWriteResult:
            self.calls += 1
            source_id = str(kwargs["source_id"])
            if self.calls == 1:
                return MemoryWriteResult(MemoryWriteStatus.SKIPPED, source_id)
            return MemoryWriteResult(
                MemoryWriteStatus.WRITTEN,
                source_id,
                ("memory.unexpected",),
            )

    memory = SkipThenWriteMemory()
    official_payload = {
        "mode": "read_only",
        "account_id": "200717",
        "letters": [
            {
                "source_record_id": "official:200717:skipped-letter",
                "source": "official-olivia",
                "occurred_at": 1710000000,
                "content": "用户来信：你好\n林离回信：你好。",
                "metadata": {
                    "user_content": "你好",
                    "reply_text": "你好。",
                    "replied_at": 1710000100,
                    "import_kind": "official_text_reply",
                    "official_account_id": "200717",
                },
            }
        ],
    }
    monkeypatch.setenv("OLIVIA_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setattr(local_server, "memory_adapter", NullMemoryPort())
    monkeypatch.setattr(local_server.letters_adapter, "memory_port", NullMemoryPort())
    monkeypatch.setattr(local_server, "conversation_memory_adapter", memory)
    monkeypatch.setattr(local_server, "private_world_port", _ExistingPrivateWorld())
    monkeypatch.setattr(
        local_server, "private_world_command_service", _ExistingHistoryAudit()
    )
    monkeypatch.setattr(
        local_server,
        "collect_default_official_text_replies",
        lambda: official_payload,
        raising=False,
    )

    first = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/legacy/official-import",
            {},
            {},
            companion_confirmed=True,
        )
    )
    duplicate = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/legacy/official-import",
            {},
            {},
            companion_confirmed=True,
        )
    )

    assert first["code"] == 0, first
    assert first["data"]["memory_migration"]["skipped"] == 1
    assert duplicate["code"] == 0, duplicate
    assert duplicate["data"]["inserted"] == 0
    assert duplicate["data"]["duplicates"] == 1
    assert duplicate["data"]["memory_migration"]["processed"] == 0
    assert memory.calls == 1


def test_official_import_retry_reuses_private_world_audit_after_archive_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import local_server

    _allow_official_history_preflight(monkeypatch, local_server)

    class PersistingMemory:
        enabled = True

        def __init__(self) -> None:
            self.calls = 0

        def status(self) -> ConversationMemoryStatus:
            return ConversationMemoryStatus(
                "available", True, "mem0", "qdrant-local"
            )

        def remember_exchange(self, **kwargs: object) -> MemoryWriteResult:
            self.calls += 1
            source_id = str(kwargs["source_id"])
            if self.calls > 1:
                return MemoryWriteResult(MemoryWriteStatus.DUPLICATE, source_id)
            return MemoryWriteResult(
                MemoryWriteStatus.WRITTEN, source_id, ("memory.synthetic",)
            )

    class FailOnceArchive:
        enabled = True

        def __init__(self) -> None:
            self.calls = 0

        def list_legacy(self) -> list[dict[str, object]]:
            return []

        def import_legacy_records(
            self,
            records,
            *,
            atomic: bool = True,
            promote_duplicate_metadata: bool = False,
        ) -> LegacyImportResult:
            del atomic, promote_duplicate_metadata
            self.calls += 1
            materialized = tuple(records)
            if self.calls == 1:
                raise OSError("synthetic archive failure")
            return LegacyImportResult(
                seen=len(materialized), inserted=len(materialized)
            )

    class CountingAssessmentGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, _messages, *, request_id=None) -> GatewayResponse:
            self.calls += 1
            return GatewayResponse(
                '{"relationship_stage":"familiar","familiarity":48,'
                '"trust":44,"comfort":42,"closeness":36,"tension":9,'
                '"evidence_indexes":[1]}',
                request_id or "request.synthetic",
                "fixture",
                "fixture-model",
            )

    memory = PersistingMemory()
    archive = FailOnceArchive()
    ledger = SQLitePrivateWorldLedger(tmp_path / "private-world.sqlite3")
    service = PrivateWorldCommandService(ledger)
    gateway = CountingAssessmentGateway()
    official_payload = build_legacy_import_payload(
        (
            {
                "letter_id": "letter-1",
                "content": "synthetic user letter",
                "reply_text": "synthetic official reply",
                "created_at": 1710000000,
                "replied_at": 1710000100,
            },
        ),
        account_id="synthetic-account",
    )

    monkeypatch.setattr(local_server, "conversation_memory_adapter", memory)
    monkeypatch.setattr(local_server, "memory_adapter", archive)
    monkeypatch.setattr(local_server, "private_world_port", ledger)
    monkeypatch.setattr(local_server, "private_world_command_service", service)
    monkeypatch.setattr(local_server.letters_adapter, "gateway", gateway)
    monkeypatch.setattr(
        local_server.letters_adapter,
        "get_persona_policy",
        lambda: "AUTHORITATIVE PERSONA POLICY",
    )
    monkeypatch.setattr(
        local_server,
        "collect_default_official_text_replies",
        lambda **_kwargs: official_payload,
    )
    monkeypatch.setattr(local_server, "_legacy_import_adapter", lambda: archive)

    def post() -> dict[str, object]:
        return asyncio.run(
            local_server.route(
                "POST",
                "/toy/letter/legacy/official-import",
                {},
                {},
                companion_confirmed=True,
            )
        )

    first, second = post(), post()

    assert first["code"] == 503
    assert first["data"]["error_code"] == "MEMORY_UNAVAILABLE"
    assert second["code"] == 0, second
    migration = second["data"]["memory_migration"]
    assert (
        migration["written"],
        migration["duplicates"],
        migration["private_world_status"],
    ) == (0, 1, "already_initialized")
    assert (memory.calls, gateway.calls, len(ledger.events()), archive.calls) == (
        2,
        1,
        1,
        2,
    )


def test_official_import_failure_returns_only_a_sanitized_error(monkeypatch) -> None:
    import local_server

    _allow_official_history_preflight(monkeypatch, local_server)

    private_value = "secret-token-and-private-letter"
    monkeypatch.setattr(
        local_server,
        "collect_default_official_text_replies",
        lambda: (_ for _ in ()).throw(ValueError(private_value)),
        raising=False,
    )

    response = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/legacy/official-import",
            {},
            {},
            companion_confirmed=True,
        )
    )

    assert response["code"] == 503
    assert response["data"] == {
        "status": "UNAVAILABLE",
        "error_code": "OFFICIAL_LETTER_IMPORT_UNAVAILABLE",
        "retryable": True,
    }
    assert private_value not in repr(response)


def test_official_import_route_rejects_invalid_timestamp_before_archiving(
    monkeypatch,
) -> None:
    import local_server

    _allow_official_history_preflight(monkeypatch, local_server)

    monkeypatch.setattr(
        local_server,
        "collect_default_official_text_replies",
        lambda: {
            "mode": "read_only",
            "account_id": "200717",
            "letters": [
                {
                    "source_record_id": "official:200717:letter-1",
                    "occurred_at": None,
                    "metadata": {
                        "user_content": "旧信",
                        "reply_text": "旧文字回信",
                        "import_kind": "official_text_reply",
                        "official_account_id": "200717",
                    },
                }
            ],
        },
    )

    response = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/legacy/official-import",
            {},
            {},
            companion_confirmed=True,
        )
    )

    assert response["code"] == 503
    assert response["data"]["error_code"] == "OFFICIAL_LETTER_IMPORT_UNAVAILABLE"


def test_official_import_requires_explicit_companion_confirmation(monkeypatch) -> None:
    import local_server

    calls: list[str] = []
    monkeypatch.setattr(
        local_server,
        "collect_default_official_text_replies",
        lambda: calls.append("collect") or {},
        raising=False,
    )

    response = asyncio.run(
        local_server.route("POST", "/toy/letter/legacy/official-import", {}, {})
    )

    assert response["code"] == 403
    assert response["data"]["error_code"] == "COMPANION_CONFIRMATION_REQUIRED"
    assert calls == []


def test_official_import_rejects_a_different_account_before_persisting(
    monkeypatch,
) -> None:
    import local_server

    _allow_official_history_preflight(monkeypatch, local_server)

    existing = {
        "letter_id": "existing",
        "source_record_id": "official:account-a:letter-1",
        "metadata": {
            "import_kind": "official_text_reply",
            "official_account_id": "account-a",
        },
        "read_only": True,
    }
    monkeypatch.setattr(
        local_server,
        "_legacy_letter_collection",
        lambda *, strict=False: [existing],
    )
    monkeypatch.setattr(
        local_server,
        "collect_default_official_text_replies",
        lambda: {"mode": "read_only", "account_id": "account-b", "letters": []},
    )

    response = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/legacy/official-import",
            {},
            {},
            companion_confirmed=True,
        )
    )

    assert response["code"] == 409
    assert response["data"]["error_code"] == "OFFICIAL_ACCOUNT_CONFLICT"


def test_official_import_fails_closed_when_account_binding_cannot_be_read(
    monkeypatch,
) -> None:
    import local_server

    _allow_official_history_preflight(monkeypatch, local_server)

    class UnreadableArchive:
        enabled = True

        def list_legacy(self):
            raise OSError("private archive path")

    monkeypatch.setattr(local_server, "memory_adapter", UnreadableArchive())
    monkeypatch.setattr(
        local_server,
        "collect_default_official_text_replies",
        lambda: {"mode": "read_only", "account_id": "account-b", "letters": []},
    )

    response = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/legacy/official-import",
            {},
            {},
            companion_confirmed=True,
        )
    )

    assert response["code"] == 503
    assert response["data"]["error_code"] == "OFFICIAL_LETTER_IMPORT_UNAVAILABLE"
