from __future__ import annotations

import asyncio
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from original_client_server import create_original_client_server_runtime


async def _run(coroutine):
    return await coroutine


def _letters() -> list[dict[str, object]]:
    return [
        {
            "letter_id": "letter.pending",
            "content": "等待中的来信",
            "letter_status": "COMPLETED",
            "audit_status": 2,
            "reply_text": "尚未公开的正文",
            "reply_mode": "text_letter",
            "reply_not_before": time.time() + 3600,
            "media_status": "NOT_REQUESTED",
            "is_read": 0,
            "created_at": 1_700_000_001,
        },
        {
            "letter_id": "letter.spoken",
            "content": "说话视频来信",
            "letter_status": "COMPLETED",
            "audit_status": 2,
            "reply_text": "已经完成的说话视频正文",
            "reply_mode": "spoken_video",
            "reply_not_before": 0.0,
            "media_status": "COMPLETED",
            "reply_video_url": (
                "http://127.0.0.1:8899/toy/media/letter.spoken.mp4"
            ),
            "is_read": 0,
            "created_at": 1_700_000_002,
            "replied_at": 1_700_000_102,
        },
        {
            "letter_id": "letter.musical",
            "content": "音乐视频来信",
            "letter_status": "COMPLETED",
            "audit_status": 2,
            "reply_text": "歌曲仍在生成，但正文已经完成。",
            "reply_mode": "musical_video",
            "reply_not_before": 0.0,
            "media_status": "PENDING",
            "reply_video_url": "",
            "is_read": 1,
            "created_at": 1_700_000_003,
        },
        {
            "letter_id": "letter.failed",
            "content": "失败来信",
            "letter_status": "FAILED",
            "audit_status": 2,
            "reply_text": "",
            "reply_mode": "text_letter",
            "reply_not_before": 0.0,
            "media_status": "NOT_REQUESTED",
            "error_code": "REPLY_QUALITY_BLOCKED",
            "retryable": False,
            "is_read": 1,
            "created_at": 1_700_000_004,
        },
    ]


def _fallback_factory(letters: list[dict[str, object]]):
    async def fallback(request: web.Request) -> web.Response:
        common_headers = {
            "Cache-Control": "no-store",
            "X-Original-Fallback": "preserved",
        }
        if request.path == "/toy/letter/list":
            if request.query.get("scope") == "invalid":
                return web.json_response(
                    {
                        "code": 400,
                        "message": "INVALID_SCOPE",
                        "data": {
                            "status": "FAILED",
                            "error_code": "INVALID_SCOPE",
                        },
                    },
                    status=400,
                    headers=common_headers,
                )
            return web.json_response(
                {
                    "code": 0,
                    "message": "ok",
                    "data": {
                        "list": [{"letter_id": value["letter_id"]} for value in letters],
                        "total": len(letters),
                        "has_more": False,
                        "next_cursor": 0,
                        "remaining_today": 17,
                        "scope": "current",
                    },
                },
                headers=common_headers,
            )
        if request.path == "/toy/letter/unread_count":
            return web.json_response(
                {
                    "code": 0,
                    "message": "ok",
                    "data": {"unread_count": 99, "scope": "current"},
                },
                headers=common_headers,
            )
        if request.path == "/toy/letter/detail":
            letter_id = request.query.get("letter_id") or request.query.get("letterId")
            value = next(
                (item for item in letters if item["letter_id"] == letter_id),
                None,
            )
            if value is None:
                return web.json_response(
                    {
                        "code": 404,
                        "message": "LETTER_NOT_FOUND",
                        "data": {
                            "status": "FAILED",
                            "error_code": "LETTER_NOT_FOUND",
                        },
                    },
                    status=404,
                    headers=common_headers,
                )
            value["is_read"] = 1
            return web.json_response(
                {
                    "code": 0,
                    "message": "ok",
                    "data": {
                        "letter_id": letter_id,
                        "reply_text": value.get("reply_text", ""),
                        "error_code": value.get("error_code"),
                        "retryable": value.get("retryable", False),
                    },
                },
                headers=common_headers,
            )
        if request.path == "/toy/letter/send":
            return web.json_response(
                {
                    "code": 0,
                    "message": "ok",
                    "data": {
                        "letter_id": "letter.spoken",
                        "status": "COMPLETED",
                    },
                },
                headers=common_headers,
            )
        return web.json_response({"fallback": request.path})

    return fallback


def test_original_collection_list_and_unread_use_camel_case_numeric_contract() -> None:
    async def scenario() -> None:
        letters = _letters()
        runtime = create_original_client_server_runtime(
            _fallback_factory(letters),
            letter_collection=lambda scope: letters if scope == "current" else (),
        )
        async with TestClient(TestServer(runtime.app)) as client:
            response = await client.get("/toy/letter/list")
            assert response.status == 200
            assert response.headers["X-Original-Fallback"] == "preserved"
            data = (await response.json())["data"]
            assert data["hasMore"] is False
            assert data["nextCursor"] == 0
            assert data["remainingToday"] == 17
            assert data["has_more"] is False
            assert data["remaining_today"] == 17
            rows = {value["letterId"]: value for value in data["list"]}
            assert rows["letter.pending"]["letterStatus"] == 1
            assert rows["letter.pending"]["replyType"] == 0
            assert rows["letter.spoken"]["letterStatus"] == 4
            assert rows["letter.spoken"]["replyType"] == 4
            assert rows["letter.musical"]["letterStatus"] == 4
            assert rows["letter.musical"]["replyType"] == 1
            assert rows["letter.failed"]["letterStatus"] == 5
            assert rows["letter.failed"]["replyType"] == 0

            unread = await client.get("/toy/letter/unread_count")
            assert unread.status == 200
            unread_data = (await unread.json())["data"]
            assert unread_data["unreadCount"] == 2
            assert unread_data["unread_count"] == 2

    asyncio.run(scenario())


def test_original_collection_detail_keeps_text_and_only_exposes_completed_local_video() -> None:
    async def scenario() -> None:
        letters = _letters()
        runtime = create_original_client_server_runtime(
            _fallback_factory(letters),
            letter_collection=lambda _scope: letters,
        )
        async with TestClient(TestServer(runtime.app)) as client:
            spoken = await client.get(
                "/toy/letter/detail?letterId=letter.spoken"
            )
            spoken_data = (await spoken.json())["data"]
            assert spoken_data["letterId"] == "letter.spoken"
            assert spoken_data["letterStatus"] == 4
            assert spoken_data["replyType"] == 4
            assert spoken_data["replyText"] == "已经完成的说话视频正文"
            assert spoken_data["replyVideoUrl"].endswith(
                "/toy/media/letter.spoken.mp4"
            )
            assert spoken_data["isRead"] == 1

            musical = await client.get(
                "/toy/letter/detail?letter_id=letter.musical"
            )
            musical_data = (await musical.json())["data"]
            assert musical_data["letterStatus"] == 4
            assert musical_data["replyType"] == 1
            assert musical_data["replyText"] == "歌曲仍在生成，但正文已经完成。"
            assert musical_data["replyVideoUrl"] == ""
            assert musical_data["media_status"] == "PENDING"

            pending = await client.get(
                "/toy/letter/detail?letter_id=letter.pending"
            )
            pending_data = (await pending.json())["data"]
            assert pending_data["letterStatus"] == 1
            assert pending_data["replyType"] == 0
            assert pending_data["replyText"] == ""
            assert pending_data["replyVideoUrl"] == ""

            failed = await client.get(
                "/toy/letter/detail?letter_id=letter.failed"
            )
            failed_data = (await failed.json())["data"]
            assert failed_data["letterStatus"] == 5
            assert failed_data["error_code"] == "REPLY_QUALITY_BLOCKED"
            assert failed_data["retryable"] is False
            assert failed_data["media_status"] == "NOT_REQUESTED"
            assert failed_data["media_error_code"] is None
            assert failed_data["media_retryable"] is False

    asyncio.run(scenario())


def test_send_adds_original_terminal_fields_without_losing_internal_status() -> None:
    async def scenario() -> None:
        letters = _letters()
        runtime = create_original_client_server_runtime(
            _fallback_factory(letters),
            letter_collection=lambda _scope: letters,
        )
        async with TestClient(TestServer(runtime.app)) as client:
            response = await client.post(
                "/toy/letter/send",
                json={"content": "synthetic"},
            )
            assert response.status == 200
            data = (await response.json())["data"]
            assert data["status"] == "COMPLETED"
            assert data["letterId"] == "letter.spoken"
            assert data["letterStatus"] == 4
            assert data["replyType"] == 4
            assert data["letter_id"] == "letter.spoken"
            assert data["letter_status"] == 4

    asyncio.run(scenario())


def test_invalid_scope_and_unknown_letter_errors_pass_through_unchanged() -> None:
    async def scenario() -> None:
        letters = _letters()
        runtime = create_original_client_server_runtime(
            _fallback_factory(letters),
            letter_collection=lambda _scope: letters,
        )
        async with TestClient(TestServer(runtime.app)) as client:
            invalid = await client.get("/toy/letter/list?scope=invalid")
            assert invalid.status == 400
            assert await invalid.json() == {
                "code": 400,
                "message": "INVALID_SCOPE",
                "data": {
                    "status": "FAILED",
                    "error_code": "INVALID_SCOPE",
                },
            }

            missing = await client.get(
                "/toy/letter/detail?letter_id=letter.missing"
            )
            assert missing.status == 404
            assert (await missing.json())["data"]["error_code"] == "LETTER_NOT_FOUND"

    asyncio.run(scenario())


def test_invalid_raw_letter_fails_closed_without_path_or_content_leak() -> None:
    async def scenario() -> None:
        runtime = create_original_client_server_runtime(
            _fallback_factory([]),
            letter_collection=lambda _scope: ({"letter_id": ""},),
        )
        async with TestClient(TestServer(runtime.app)) as client:
            response = await client.get("/toy/letter/list")
            assert response.status == 503
            assert await response.json() == {
                "code": 503,
                "message": "ORIGINAL_CLIENT_MAILBOX_UNAVAILABLE",
                "data": {
                    "status": "UNAVAILABLE",
                    "error_code": "ORIGINAL_CLIENT_MAILBOX_UNAVAILABLE",
                },
            }

    asyncio.run(scenario())
