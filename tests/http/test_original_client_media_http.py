from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

import original_client_media_http as legacy_media_http
from runtime.original_client_media_http import (
    OriginalClientMediaError,
    ResolvedReplyMedia,
    mount_reply_media_route,
    original_webplayer_uid,
)


def test_legacy_module_reexports_canonical_adapter() -> None:
    assert legacy_media_http.original_webplayer_uid is original_webplayer_uid


class MappingResolver:
    def __init__(self, values: dict[str, ResolvedReplyMedia]) -> None:
        self.values = values
        self.requests: list[str] = []

    def resolve_reply_media(self, media_id: str) -> ResolvedReplyMedia | None:
        self.requests.append(media_id)
        return self.values.get(media_id)


def _client(
    tmp_path: Path,
    *,
    content: bytes = b"0123456789abcdefghijklmnopqrstuvwxyz",
) -> tuple[web.Application, MappingResolver, Path]:
    media_root = tmp_path / "media"
    media_root.mkdir()
    media = media_root / "reply.mp4"
    media.write_bytes(content)
    resolver = MappingResolver(
        {"letter-1:revision-2": ResolvedReplyMedia("letter-1:revision-2", media)}
    )
    app = web.Application()
    mount_reply_media_route(app, resolver=resolver, media_root=media_root)
    return app, resolver, media


def test_original_webplayer_uid_accepts_only_explicit_loopback_base() -> None:
    assert original_webplayer_uid(
        "http://127.0.0.1:8899", "letter-1:revision-2"
    ) == "http://127.0.0.1:8899/toy/media/letter-1:revision-2"
    assert original_webplayer_uid(
        "http://localhost:8899/", "letter.1"
    ) == "http://localhost:8899/toy/media/letter.1"

    invalid = (
        "https://127.0.0.1:8899",
        "http://example.invalid:8899",
        "http://127.0.0.1",
        "http://user@127.0.0.1:8899",
        "http://127.0.0.1:8899/base",
        "http://127.0.0.1:8899?token=x",
    )
    for value in invalid:
        with pytest.raises(OriginalClientMediaError) as error:
            original_webplayer_uid(value, "letter-1")
        assert error.value.code == "MEDIA_BASE_URL_INVALID"

    with pytest.raises(OriginalClientMediaError) as identifier:
        original_webplayer_uid("http://127.0.0.1:8899", "../reply")
    assert identifier.value.code == "MEDIA_ID_INVALID"


def test_media_route_serves_full_get_head_and_etag(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, resolver, media = _client(tmp_path)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/toy/media/letter-1:revision-2")
            assert response.status == 200
            assert await response.read() == media.read_bytes()
            assert response.headers["Accept-Ranges"] == "bytes"
            assert response.headers["Content-Type"].startswith("video/mp4")
            assert response.headers["Content-Length"] == str(media.stat().st_size)
            assert response.headers["Cache-Control"] == "private, no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            etag = response.headers["ETag"]

            head = await client.head("/toy/media/letter-1:revision-2")
            assert head.status == 200
            assert await head.read() == b""
            assert head.headers["ETag"] == etag
            assert head.headers["Content-Length"] == str(media.stat().st_size)

            unchanged = await client.get(
                "/toy/media/letter-1:revision-2",
                headers={"If-None-Match": etag},
            )
            assert unchanged.status == 304
            assert await unchanged.read() == b""
            assert resolver.requests == [
                "letter-1:revision-2",
                "letter-1:revision-2",
                "letter-1:revision-2",
            ]

    asyncio.run(scenario())


def test_media_route_supports_open_closed_full_and_suffix_ranges(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, _resolver, _media = _client(tmp_path, content=b"0123456789")
        async with TestClient(TestServer(app)) as client:
            cases = (
                ("bytes=2-5", b"2345", "bytes 2-5/10"),
                ("bytes=7-", b"789", "bytes 7-9/10"),
                ("bytes=-3", b"789", "bytes 7-9/10"),
                ("bytes=8-99", b"89", "bytes 8-9/10"),
                ("bytes=0-9", b"0123456789", "bytes 0-9/10"),
            )
            for requested, expected, content_range in cases:
                response = await client.get(
                    "/toy/media/letter-1:revision-2",
                    headers={"Range": requested},
                )
                assert response.status == 206
                assert await response.read() == expected
                assert response.headers["Content-Range"] == content_range
                assert response.headers["Content-Length"] == str(len(expected))

    asyncio.run(scenario())


def test_media_route_rejects_invalid_ranges_and_unknown_ids(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, _resolver, _media = _client(tmp_path, content=b"0123456789")
        async with TestClient(TestServer(app)) as client:
            for value in (
                "items=0-1",
                "bytes=",
                "bytes=9-2",
                "bytes=20-",
                "bytes=0-1,3-4",
            ):
                response = await client.get(
                    "/toy/media/letter-1:revision-2",
                    headers={"Range": value},
                )
                assert response.status == 416
                assert (await response.json())["error_code"] == "MEDIA_RANGE_INVALID"
                assert response.headers["Content-Range"] == "bytes */10"

            missing = await client.get("/toy/media/unknown")
            assert missing.status == 404
            assert (await missing.json()) == {
                "status": "FAILED",
                "error_code": "MEDIA_NOT_FOUND",
            }

            malformed = await client.get("/toy/media/..%2Freply")
            assert malformed.status in {404, 405}

    asyncio.run(scenario())


def test_media_route_confines_resolved_files_to_media_root(tmp_path: Path) -> None:
    async def scenario() -> None:
        media_root = tmp_path / "media"
        media_root.mkdir()
        outside = tmp_path / "outside.mp4"
        outside.write_bytes(b"private")
        resolver = MappingResolver({"outside": ResolvedReplyMedia("outside", outside)})
        app = web.Application()
        mount_reply_media_route(app, resolver=resolver, media_root=media_root)

        async with TestClient(TestServer(app)) as client:
            response = await client.get("/toy/media/outside")
            assert response.status == 404
            payload = await response.json()
            assert payload["error_code"] == "MEDIA_NOT_FOUND"
            assert str(outside) not in str(payload)

    asyncio.run(scenario())


def test_media_route_rejects_empty_non_video_and_invalid_resolver_results(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        media_root = tmp_path / "media"
        media_root.mkdir()
        empty = media_root / "empty.mp4"
        empty.write_bytes(b"")
        text = media_root / "reply.txt"
        text.write_text("not media", encoding="utf-8")

        class InvalidResolver:
            def resolve_reply_media(self, media_id: str):
                if media_id == "empty":
                    return ResolvedReplyMedia("empty", empty)
                if media_id == "text":
                    return ResolvedReplyMedia("text", text)
                if media_id == "wrong":
                    return ResolvedReplyMedia("different", empty)
                raise OSError("private path")

        app = web.Application()
        mount_reply_media_route(app, resolver=InvalidResolver(), media_root=media_root)
        async with TestClient(TestServer(app)) as client:
            for media_id in ("empty", "text"):
                response = await client.get(f"/toy/media/{media_id}")
                assert response.status == 404
                assert (await response.json())["error_code"] == "MEDIA_NOT_FOUND"

            wrong = await client.get("/toy/media/wrong")
            assert wrong.status == 503
            assert (await wrong.json())["error_code"] == "MEDIA_RESOLVER_INVALID"

            failed = await client.get("/toy/media/failed")
            assert failed.status == 503
            payload = await failed.json()
            assert payload["error_code"] == "MEDIA_RESOLVER_UNAVAILABLE"
            assert "private path" not in str(payload)

    asyncio.run(scenario())


def test_media_route_mounts_once_and_validates_chunk_size(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    media_root.mkdir()
    resolver = MappingResolver({})
    app = web.Application()
    mount_reply_media_route(app, resolver=resolver, media_root=media_root)

    with pytest.raises(RuntimeError, match="MEDIA_ROUTE_ALREADY_MOUNTED"):
        mount_reply_media_route(app, resolver=resolver, media_root=media_root)

    other = web.Application()
    with pytest.raises(ValueError, match="chunk size"):
        mount_reply_media_route(
            other,
            resolver=resolver,
            media_root=media_root,
            chunk_size=1024,
        )
