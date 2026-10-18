"""The interface language as the backend sees it: state, saved choice, the local report route and the cloud header."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from original_client_setup_api import LLMSetupService, mount_original_client_setup_api
from original_client_ui_language_api import UI_LANGUAGE_PATH, mount_ui_language_api
from runtime import ui_language

TRUSTED_ORIGIN = "https://client.example"
CONFIRM = {"X-Olivia-Setup-Action": "confirmed"}


# --- state and persistence ---------------------------------------------------------------------------------------

def test_the_default_is_chinese_and_nothing_is_added_to_requests():
    assert ui_language.current() == "zh-cn"
    assert not ui_language.is_english()
    assert ui_language.headers() == {}
    existing = {"Authorization": "Bearer x"}
    # Chinese: the very same object comes back, so existing requests are untouched
    assert ui_language.with_header(existing) is existing
    assert ui_language.with_header(None) is None


def test_english_adds_exactly_one_header_and_never_changes_the_input():
    ui_language.set_language("en")
    assert ui_language.current() == "en" and ui_language.is_english()
    assert ui_language.headers() == {"X-Olivia-UI-Language": "en"}
    existing = {"Authorization": "Bearer x"}
    merged = ui_language.with_header(existing)
    assert merged == {"Authorization": "Bearer x", "X-Olivia-UI-Language": "en"}
    assert existing == {"Authorization": "Bearer x"}
    assert dict(ui_language.with_header(None)) == {"X-Olivia-UI-Language": "en"}


@pytest.mark.parametrize("value", ["fr", "EN-us", "", None, 5, ["en"], "english"])
def test_only_the_two_known_values_are_accepted(value):
    with pytest.raises(ValueError):
        ui_language.set_language(value)
    assert ui_language.current() == "zh-cn"


def test_values_are_matched_case_insensitively_and_trimmed():
    assert ui_language.set_language(" EN ")[0] == "en"
    assert ui_language.set_language("ZH-CN")[0] == "zh-cn"


def test_the_choice_is_saved_and_survives_a_restart(tmp_path):
    path = tmp_path / "config" / "ui-language.json"
    assert ui_language.configure(path) == "zh-cn"
    assert ui_language.set_language("en") == ("en", True)
    assert json.loads(path.read_text(encoding="utf-8")) == {"language": "en", "schema_version": 1}
    ui_language.reset()
    assert ui_language.current() == "zh-cn"
    assert ui_language.configure(path) == "en"
    assert ui_language.read_saved(path) == "en"
    ui_language.set_language("zh-cn")
    ui_language.reset()
    assert ui_language.configure(path) == "zh-cn"


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"language": "en"}', '{"schema_version": 2, "language": "en"}',
                                     '{"schema_version": 1, "language": "fr"}'])
def test_a_missing_or_damaged_file_means_chinese(tmp_path, content):
    path = tmp_path / "ui-language.json"
    assert ui_language.configure(path) == "zh-cn"  # missing
    path.write_text(content, encoding="utf-8")
    ui_language.reset()
    assert ui_language.configure(path) == "zh-cn"
    assert ui_language.read_saved(path) is None


def test_reporting_the_same_language_again_does_not_rewrite_the_file(tmp_path, monkeypatch):
    path = tmp_path / "ui-language.json"
    ui_language.configure(path)
    writes = []
    original = ui_language._persist
    monkeypatch.setattr(ui_language, "_persist", lambda language: writes.append(language) or original(language))
    ui_language.set_language("en")
    ui_language.set_language("en")
    ui_language.set_language("en")
    assert writes == ["en"]


def test_a_file_that_cannot_be_written_keeps_the_choice_for_this_run(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where a folder is needed", encoding="utf-8")
    ui_language.configure(blocked / "ui-language.json")
    assert ui_language.set_language("en") == ("en", False)
    assert ui_language.current() == "en"


def test_listeners_hear_about_real_changes_only(tmp_path):
    ui_language.configure(tmp_path / "ui-language.json")
    heard = []
    ui_language.add_listener(heard.append)
    ui_language.add_listener(heard.append)  # registered once
    ui_language.set_language("zh-cn")
    ui_language.set_language("en")
    ui_language.set_language("en")
    ui_language.set_language("zh-cn")
    assert heard == ["en", "zh-cn"]
    ui_language.remove_listener(heard.append)


def test_a_failing_listener_does_not_stop_the_change(tmp_path):
    ui_language.configure(tmp_path / "ui-language.json")
    heard = []

    def broken(_language):
        raise RuntimeError("boom")

    ui_language.add_listener(broken)
    ui_language.add_listener(heard.append)
    assert ui_language.set_language("en") == ("en", True)
    assert heard == ["en"]


def test_child_processes_inherit_the_language(tmp_path):
    assert ui_language.ENVIRONMENT_NAME not in os.environ
    ui_language.set_language("en")
    assert os.environ[ui_language.ENVIRONMENT_NAME] == "en"
    ui_language.reset()
    # a process that never loaded the saved file (a worker) falls back to what its parent exported
    os.environ[ui_language.ENVIRONMENT_NAME] = "en"
    assert ui_language.current() == "en" and ui_language.headers() == {"X-Olivia-UI-Language": "en"}
    os.environ[ui_language.ENVIRONMENT_NAME] = "garbage"
    assert ui_language.current() == "zh-cn"
    ui_language.set_language("zh-cn")
    assert ui_language.ENVIRONMENT_NAME not in os.environ


# --- the local route the page reports to --------------------------------------------------------------------------

def _service(tmp_path: Path) -> LLMSetupService:
    return LLMSetupService(tmp_path, protect=lambda value: value, unprotect=lambda value: value)


def _app(tmp_path: Path) -> web.Application:
    app = web.Application()
    mount_original_client_setup_api(app, _service(tmp_path), trusted_origins=(TRUSTED_ORIGIN,))
    mount_ui_language_api(app, _service(tmp_path))
    return app


def test_the_route_reports_saves_and_reads_back_the_language(tmp_path):
    async def scenario() -> None:
        app = _app(tmp_path)
        async with TestClient(TestServer(app)) as client:
            read = await client.get(UI_LANGUAGE_PATH, headers={"Origin": TRUSTED_ORIGIN})
            assert read.status == 200 and await read.json() == {"status": "READY", "language": "zh-cn"}
            saved = await client.post(UI_LANGUAGE_PATH, headers={"Origin": TRUSTED_ORIGIN, **CONFIRM}, json={"language": "en"})
            assert saved.status == 200
            assert await saved.json() == {"status": "SAVED", "language": "en", "persisted": True}
            assert ui_language.current() == "en"
            assert (tmp_path / "config" / "ui-language.json").is_file()
            read = await client.get(UI_LANGUAGE_PATH, headers={"Origin": TRUSTED_ORIGIN})
            assert (await read.json())["language"] == "en"
            back = await client.post(UI_LANGUAGE_PATH, headers={"Origin": TRUSTED_ORIGIN, **CONFIRM}, json={"language": "zh-cn"})
            assert (await back.json())["language"] == "zh-cn" and ui_language.current() == "zh-cn"

    asyncio.run(scenario())


def test_the_route_is_limited_to_the_trusted_page_and_needs_the_confirmation_header(tmp_path):
    async def scenario() -> None:
        app = _app(tmp_path)
        async with TestClient(TestServer(app)) as client:
            foreign = await client.post(UI_LANGUAGE_PATH, headers={"Origin": "https://elsewhere.example", **CONFIRM}, json={"language": "en"})
            assert foreign.status == 403 and (await foreign.json())["error_code"] == "LLM_SETUP_ORIGIN_FORBIDDEN"
            nothing = await client.post(UI_LANGUAGE_PATH, json={"language": "en"})
            assert nothing.status == 403
            unconfirmed = await client.post(UI_LANGUAGE_PATH, headers={"Origin": TRUSTED_ORIGIN}, json={"language": "en"})
            assert unconfirmed.status == 403
            assert (await unconfirmed.json())["error_code"] == "LLM_SETUP_CONFIRMATION_REQUIRED"
            assert ui_language.current() == "zh-cn"

    asyncio.run(scenario())


@pytest.mark.parametrize("body", [{"language": "fr"}, {"language": 7}, {}, {"language": "en", "extra": 1}, {"lang": "en"}])
def test_the_route_rejects_other_values_and_extra_fields(tmp_path, body):
    async def scenario() -> None:
        app = _app(tmp_path)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(UI_LANGUAGE_PATH, headers={"Origin": TRUSTED_ORIGIN, **CONFIRM}, json=body)
            assert response.status == 400 and (await response.json())["error_code"] == "LLM_SETUP_FIELDS_INVALID"
            assert ui_language.current() == "zh-cn"

    asyncio.run(scenario())


def test_the_route_answers_the_cors_preflight_of_the_page(tmp_path):
    async def scenario() -> None:
        app = _app(tmp_path)
        async with TestClient(TestServer(app)) as client:
            response = await client.options(UI_LANGUAGE_PATH, headers={"Origin": TRUSTED_ORIGIN})
            assert response.status == 204
            assert response.headers["Access-Control-Allow-Origin"] == TRUSTED_ORIGIN
            assert "X-Olivia-Setup-Action" in response.headers["Access-Control-Allow-Headers"]
            assert "POST" in response.headers["Access-Control-Allow-Methods"]

    asyncio.run(scenario())


def test_the_saved_language_is_loaded_when_the_server_starts(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "ui-language.json").write_text('{"language": "en", "schema_version": 1}', encoding="utf-8")

    async def scenario() -> None:
        app = _app(tmp_path)
        async with TestClient(TestServer(app)) as client:
            assert ui_language.current() == "en"
            read = await client.get(UI_LANGUAGE_PATH, headers={"Origin": TRUSTED_ORIGIN})
            assert (await read.json())["language"] == "en"

    asyncio.run(scenario())


def test_the_real_server_mounts_the_route_after_the_setup_routes():
    source = (Path(__file__).parents[2] / "original_client_server.py").read_text(encoding="utf-8")
    assert source.index("mount_original_client_setup_api(") < source.index("mount_ui_language_api(app, setup_service)")
    # before the catch-all fallback, which would otherwise answer first
    assert source.index("mount_ui_language_api(app, setup_service)") < source.index('app.router.add_route("*", "/{tail:.*}"')
