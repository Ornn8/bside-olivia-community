"""Calls to Olivia's cloud services carry the interface language in English only; Chinese traffic is unchanged."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from runtime import ui_language
from runtime.cloud_service import CloudError

HEADER = "X-Olivia-UI-Language"
ROOT = Path(__file__).parents[2]


# --- a recorder that stands in for aiohttp.ClientSession ----------------------------------------------------------

class _Stream:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def iter_chunked(self, size: int):
        yield self.body

    async def read(self, count: int = -1) -> bytes:
        return self.body


class _Response:
    status = 200
    headers: dict = {}

    def __init__(self, body: bytes) -> None:
        self.content = _Stream(body)
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self) -> None:
        return None

    async def json(self, **_kw):
        return json.loads(self._body)


def recorder(sent: list, body: bytes = b"{}"):
    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def request(self, method, url, **kwargs):
            sent.append({"method": method, "url": url, "headers": dict(kwargs.get("headers") or {})})
            return _Response(body)

        def get(self, url, **kwargs):
            return self.request("GET", url, **kwargs)

        def post(self, url, **kwargs):
            return self.request("POST", url, **kwargs)

    return Session


async def _loopback(handler):
    """A real local HTTP server that records the headers it receives."""
    seen: list[dict] = []

    async def record(request: web.Request) -> web.Response:
        seen.append(dict(request.headers))
        return await handler(request)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", record)
    server = TestServer(app)
    await server.start_server()
    return server, seen


def _olivia_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower().startswith("x-olivia-") or k == "Authorization"}


# --- the LLM gateway and the relay --------------------------------------------------------------------------------

def test_llm_gateway_headers_are_unchanged_in_chinese_and_add_one_header_in_english():
    from llm_gateway import provider_request_headers

    base = provider_request_headers("https://api.bside-moon.cn/v1")
    assert base == {"User-Agent": "Olivia-Community/0.1", "X-Olivia-Chat-Price": "account-20261017"}
    ui_language.set_language("en")
    assert provider_request_headers("https://api.bside-moon.cn/v1") == {**base, HEADER: "en"}


def test_relay_requests_carry_the_header_in_english_only(monkeypatch):
    import original_client_relay_api as relay

    sent: list = []
    monkeypatch.setattr(relay, "ClientSession", recorder(sent))

    async def call():
        await relay.relay_request(relay.RELAY_BASE, "olivia-synthetic-key", "GET", "/models")

    asyncio.run(call())
    assert sent[0]["headers"] == {"Authorization": "Bearer olivia-synthetic-key", "X-Olivia-Chat-Price": "account-20261017",
                                  "X-Olivia-JEV-Price": "jev-input-cny-20261017-v6"}
    ui_language.set_language("en")
    asyncio.run(call())
    assert sent[1]["headers"] == {**sent[0]["headers"], HEADER: "en"}


def test_the_policy_fetch_carries_the_header_in_english_only(monkeypatch):
    from runtime import cloud_policy

    sent: list = []
    monkeypatch.setattr(aiohttp, "ClientSession", recorder(sent))
    asyncio.run(cloud_policy.fetch_policy("olivia-synthetic-key", "2.4.5"))
    assert sent[0]["headers"] == {"Authorization": "Bearer olivia-synthetic-key", "X-Olivia-Client-Version": "2.4.5"}
    ui_language.set_language("en")
    asyncio.run(cloud_policy.fetch_policy("olivia-synthetic-key", "2.4.5"))
    assert sent[1]["headers"] == {**sent[0]["headers"], HEADER: "en"}


# --- the generation service, the call service ---------------------------------------------------------------------

def test_generation_requests_over_a_real_connection(monkeypatch):
    from runtime.remote_generation import RemoteGeneration

    async def scenario():
        async def answer(request):
            return web.json_response({"kinds": [], "shared_assets": []})

        server, seen = await _loopback(answer)
        try:
            api = RemoteGeneration(f"http://127.0.0.1:{server.port}", "olivia-synthetic-key")
            await api.request("capabilities", {})
            ui_language.set_language("en")
            await api.request("capabilities", {})
            try:
                await api.request("wardrobe_get", {})  # the empty answer is not a valid wardrobe; only the request matters
            except CloudError:
                pass
        finally:
            await server.close()
        return seen

    zh, en, en_other = asyncio.run(scenario())
    expected = {"Authorization": "Bearer olivia-synthetic-key", "X-Olivia-Wardrobe-Protocol": "daily-v2",
                "X-Olivia-Wardrobe-Catalog": "dynamic-v1", "X-Olivia-Daily-Video-Moment": "1", "X-Olivia-Piano": "1",
                "X-Olivia-Doodle": "1"}
    assert _olivia_headers(zh) == expected
    assert HEADER not in zh and not any(name.lower() == HEADER.lower() for name in zh)
    assert _olivia_headers(en) == {**expected, HEADER: "en"}
    assert en_other[HEADER] == "en"


def test_the_wardrobe_picture_download_carries_the_header_in_english_only():
    from runtime.remote_generation import RemoteGeneration

    async def scenario():
        async def answer(request):
            return web.Response(body=b"\x89PNG\r\n\x1a\n" + b"0" * 16, content_type="image/png")

        server, seen = await _loopback(answer)
        try:
            api = RemoteGeneration(f"http://127.0.0.1:{server.port}", "olivia-synthetic-key")
            await api.wardrobe_image("original-01")
            ui_language.set_language("en")
            await api.wardrobe_image("original-01")
        finally:
            await server.close()
        return seen

    zh, en = asyncio.run(scenario())
    assert _olivia_headers(zh) == {"Authorization": "Bearer olivia-synthetic-key", "X-Olivia-Wardrobe-Protocol": "daily-v2"}
    assert _olivia_headers(en) == {**_olivia_headers(zh), HEADER: "en"}


def test_call_service_requests_carry_the_header_in_english_only(monkeypatch):
    from runtime.call import http as call_http

    async def scenario():
        async def answer(request):
            return web.json_response({})

        server, seen = await _loopback(answer)
        monkeypatch.setenv("OLIVIA_GPU_API_URL", f"http://127.0.0.1:{server.port}")
        monkeypatch.setenv("OLIVIA_GPU_API_KEY", "olivia-synthetic-key")
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            await call_http._cloud_json("GET", "/v1/call/config", timeout=timeout)
            await call_http._cloud_json("GET", "/v1/call/phones", timeout=timeout, headers={"Authorization": "Bearer olivia-synthetic-key"})
            ui_language.set_language("en")
            await call_http._cloud_json("GET", "/v1/call/config", timeout=timeout)
            await call_http._cloud_json("GET", "/v1/call/phones", timeout=timeout, headers={"Authorization": "Bearer olivia-synthetic-key"})
        finally:
            await server.close()
        return seen

    plain, keyed, plain_en, keyed_en = asyncio.run(scenario())
    assert _olivia_headers(plain) == {} and _olivia_headers(keyed) == {"Authorization": "Bearer olivia-synthetic-key"}
    assert _olivia_headers(plain_en) == {HEADER: "en"}
    assert _olivia_headers(keyed_en) == {"Authorization": "Bearer olivia-synthetic-key", HEADER: "en"}


# --- the JEV decision service, embeddings and the anonymous upload ----------------------------------------------

def test_jev_requests_carry_the_header_in_english_only(monkeypatch):
    from original_client_relay_api import RELAY_BASE
    from runtime.reply import jev_billing
    from runtime.reply.companion_decision import JevDecisionPort

    monkeypatch.setattr(jev_billing, "_account_key", lambda: "olivia-synthetic-cloud-key")
    digest = hashlib.sha256(b"{}").hexdigest()
    port = JevDecisionPort(RELAY_BASE + "/companion/decide")
    zh = port.request_headers(digest)
    assert HEADER not in zh
    ui_language.set_language("en")
    en = port.request_headers(digest)
    assert en[HEADER] == "en" and {k: v for k, v in en.items() if k != HEADER}.keys() == zh.keys()
    # the local development transport never talks to the cloud and gets nothing added
    local = JevDecisionPort("http://127.0.0.1:9/v1/companion/decide", token="local-test").request_headers(digest)
    assert HEADER not in local


def test_embedding_requests_carry_the_header_in_english_only(monkeypatch):
    import urllib.request

    from runtime.memory import remote_embedding

    sent: list = []

    class Opener:
        def open(self, request, timeout=None):
            sent.append(dict(request.header_items()))
            body = json.dumps({"data": [{"index": 0, "embedding": [1.0, 0.0]}]}).encode()

            class Reply:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def read(self, count=-1):
                    return body

            return Reply()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: Opener())
    remote_embedding._post("olivia-synthetic-key", ["a"], 5, "document")
    ui_language.set_language("en")
    remote_embedding._post("olivia-synthetic-key", ["a"], 5, "document")
    zh, en = sent
    assert "X-olivia-ui-language" not in zh and not any(name.lower() == HEADER.lower() for name in zh)
    assert {name.lower() for name in en} - {name.lower() for name in zh} == {HEADER.lower()}


def test_the_anonymous_upload_carries_the_header_in_english_only(monkeypatch):
    import urllib.request

    from runtime.improve import upload

    sent: list = []

    class Reply:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, count=-1):
            return b"{}"

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: sent.append(dict(request.header_items())) or Reply())
    upload.post_json("https://api.bside-moon.cn/v1/improve", "/ping", {"a": 1})
    ui_language.set_language("en")
    upload.post_json("https://api.bside-moon.cn/v1/improve", "/ping", {"a": 1})
    zh, en = sent
    assert not any(name.lower() == HEADER.lower() for name in zh)
    assert {name.lower() for name in en} - {name.lower() for name in zh} == {HEADER.lower()}


# --- no call to the cloud is left out ------------------------------------------------------------------------------

# relay_session.session( is the kept-alive session model calls borrow; its caller still sets the headers.
CLIENT_PATTERN = re.compile(
    r"ClientSession\(|urlopen\(|build_opener\(|http\.client\.HTTP\w*Connection|\.ws_connect\(|urllib\.request\.Request\(|\bRequest\("
    r"|relay_session\.session\("
)

# Files that call Olivia's own services (api.bside-moon.cn): each must put the language header on its requests.
CLOUD_CLIENTS = {
    "llm_gateway.py", "original_client_relay_api.py", "runtime/remote_generation.py", "runtime/call/http.py",
    "runtime/call/desk.py", "runtime/cloud_policy.py", "runtime/image_assets.py", "runtime/image_understanding.py",
    "runtime/relationship_batch.py", "runtime/memory/remote_embedding.py", "runtime/reply/companion_decision.py",
    "runtime/reply/jev_billing.py", "runtime/improve/upload.py", "runtime/personal_chat/napcat_bundle.py",
    "runtime/incoming_media.py",
}
# Files whose requests go elsewhere, with the reason. Their headers are not ours to extend.
OTHER_CLIENTS = {
    "asr/provider.py": "the speech recogniser the user runs locally",
    "installer/build_offline_core_assets.py": "build tooling",
    "installer/bundle_media_tool.py": "build tooling",
    "installer/start_local.py": "loopback health check of the local backend",
    "mem0_capability_install.py": "model and package mirrors",
    "original_client_update_api.py": "signed component updates from the update source",
    "runtime/auto_update.py": "the signed update manifest on the public site",
    "runtime/cloud_contact.py": "a contact's fixed photo from the public site, checked by size and sha256",
    "runtime/cloud_service.py": "the optional account connection to an address the user entered",
    "runtime/imports/official_letters.py": "the original game's own service",
    "runtime/media/music_reply.py": "the music generator running on this computer",
    "runtime/personal_chat/bilibili.py": "public video pages and media, fetched on this computer",
    "runtime/personal_chat/napcat_installer.py": "QQ protocol software on this computer, public download",
    "runtime/personal_chat/probe.py": "QQ and WeChat transports",
    "runtime/personal_chat/qq.py": "QQ transport on this computer",
    "runtime/personal_chat/qzone.py": "QQ space pages with the session QQ already holds",
    "runtime/personal_chat/setup.py": "QQ and WeChat transports",
    "runtime/personal_chat/wechat.py": "WeChat transport",
    "runtime/private_world/student_world.py": "a public weather service",
    "runtime/relay_session.py": "kept-alive transport for llm_gateway; the caller sets every header",
    "runtime/reply/jev_http.py": "kept-alive transport for JevDecisionPort and settlements; the callers set every header",
    "runtime/reply/companion_duties.py": "uses JevDecisionPort.request_headers",
    "runtime/reply/jev_questions.py": "uses JevDecisionPort.request_headers",
    "runtime/reply/semantic_shadow.py": "a loopback development endpoint",
    "video_capability_install.py": "runtime package mirrors",
}


def _http_client_files() -> set[str]:
    found = set()
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(("tests/", "tools/", "scripts/", "control_center/", ".evidence/")):
            continue
        if CLIENT_PATTERN.search(path.read_text(encoding="utf-8")):
            found.add(relative)
    return found


def test_every_http_client_is_classified():
    unclassified = _http_client_files() - CLOUD_CLIENTS - set(OTHER_CLIENTS)
    assert not unclassified, (
        "These files create HTTP requests. If they call Olivia's cloud, add `ui_language.headers()` to the request "
        f"headers and list them in CLOUD_CLIENTS; otherwise list them in OTHER_CLIENTS with the reason: {sorted(unclassified)}"
    )
    assert not (CLOUD_CLIENTS | set(OTHER_CLIENTS)) - _http_client_files() - {"runtime/reply/companion_duties.py"} - {
        "runtime/reply/jev_questions.py"}, "a classified file no longer creates HTTP requests"


@pytest.mark.parametrize("relative", sorted(CLOUD_CLIENTS))
def test_every_cloud_client_adds_the_language_header(relative):
    source = (ROOT / relative).read_text(encoding="utf-8")
    assert "ui_language" in source
    assert "ui_language.headers()" in source or "ui_language.with_header(" in source


def test_the_header_is_added_by_one_helper_and_spelled_in_one_place():
    spelled = [path.relative_to(ROOT).as_posix() for path in ROOT.rglob("*.py")
               if HEADER in path.read_text(encoding="utf-8") and not path.relative_to(ROOT).as_posix().startswith(("tests/", "tools/"))]
    assert spelled == ["runtime/ui_language.py"]
