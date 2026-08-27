from __future__ import annotations

import asyncio

import pytest

from memory_port import NullMemoryPort
from runtime.imports.official_letters import (
    build_legacy_import_payload,
    collect_official_text_replies,
)


def test_official_text_replies_become_read_only_legacy_pairs() -> None:
    payload = build_legacy_import_payload(
        [
            {
                "letter_id": "letter-text",
                "created_at": 1710000000,
                "replied_at": 1710000100,
                "content": "用户写下的旧信",
                "reply_text": "林离过去的文字回信",
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
                "data": {"list": [{"letter_id": "letter-1"}], "has_more": False},
            }
        return {
            "code": 0,
            "data": {
                "letter_id": "letter-1",
                "created_at": 1710000000,
                "replied_at": 1710000100,
                "content": "旧信正文",
                "reply_text": "旧文字回信",
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
                        "list": [{"letter_id": "letter-1"}],
                        "has_more": True,
                        "next_cursor": 50,
                    },
                }
            return {
                "code": 0,
                "data": {"list": [{"letter_id": "letter-2"}], "has_more": False},
            }
        letter_id = path.rsplit("=", 1)[-1]
        return {
            "code": 0,
            "data": {
                "letter_id": letter_id,
                "content": f"旧信 {letter_id}",
                "reply_text": f"回信 {letter_id}",
            },
        }

    payload = collect_official_text_replies(log_path, request_json=request_json)

    assert list_paths == [
        "/letter/list?cursor=0&page_size=50",
        "/letter/list?cursor=50&page_size=50",
    ]
    assert len(payload["letters"]) == 2


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


def test_official_import_route_keeps_read_only_pair_in_legacy_mailbox(
    tmp_path,
    monkeypatch,
) -> None:
    import local_server

    monkeypatch.setenv("OLIVIA_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setattr(local_server, "memory_adapter", NullMemoryPort())
    monkeypatch.setattr(local_server.letters_adapter, "memory_port", NullMemoryPort())
    monkeypatch.setattr(
        local_server,
        "collect_default_official_text_replies",
        lambda: {
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
                    },
                }
            ],
        },
        raising=False,
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
    letter_id = listed["data"]["list"][0]["letter_id"]
    detail = asyncio.run(
        local_server.route(
            "GET",
            "/toy/letter/detail",
            {},
            {"scope": "legacy", "letter_id": letter_id},
        )
    )

    assert imported["code"] == 0
    assert imported["data"]["inserted"] == 1
    assert duplicate["code"] == 0
    assert duplicate["data"]["inserted"] == 0
    assert duplicate["data"]["duplicates"] == 1
    assert current["data"]["total"] == 0
    assert current_unread["data"]["unread_count"] == 0
    assert listed["data"]["total"] == 1
    assert listed["data"]["list"][0]["summary"] == "旧信正文"
    assert detail["data"]["content"] == "旧信正文"
    assert detail["data"]["reply_text"] == "旧文字回信"
    assert detail["data"]["reply_video_url"] == ""
    assert detail["data"]["read_only"] is True


def test_official_import_failure_returns_only_a_sanitized_error(monkeypatch) -> None:
    import local_server

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
