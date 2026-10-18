"""The page reports its language to the backend, and pages that show backend text read well in English.

Browser tests; they are skipped where Playwright is not installed (the public CI has none).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import time

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
from runtime import ui_language
from runtime.ui_i18n import I18N_JAVASCRIPT

HAN = re.compile(r"[一-鿿]")


def _phase_one():
    """The browser harness of the English interface tests (synthetic answers of the local service)."""
    path = Path(__file__).with_name("test_ui_i18n.py")
    spec = importlib.util.spec_from_file_location("olivia_english_interface_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harness = _phase_one()
ORIGIN = harness.ORIGIN


def _boot(browser, language=None, *, on_language=None, width=1100, height=1000):
    """The whole injected script in a page. ``on_language(route, body)`` answers POST /api/ui-language."""
    page = browser.new_page(viewport={"width": width, "height": height}, locale="en-US")
    if language:
        page.add_init_script(f"try{{localStorage.setItem('olivia.ui.language','{language}')}}catch(_){{}}")
    errors = []
    posts = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.errors, page.posts = errors, posts
    state = harness.CHAT_STATES["choice"]

    def handle(route):
        request = route.request
        path = request.url[len(ORIGIN):].split("?")[0]
        if path == "/assets/olivia-companion-settings.js":
            return route.fulfill(content_type="application/javascript", body=BOOTSTRAP_JAVASCRIPT)
        if path == "/":
            return route.fulfill(content_type="text/html", body=harness.PAGE % {"origin": ORIGIN, "chat": harness.PERSONAL_CHAT_SETUP_JAVASCRIPT})
        try:
            body = request.post_data_json if request.method == "POST" else None
        except Exception:  # noqa: BLE001
            body = None
        if path == "/api/ui-language":
            posts.append({"language": (body or {}).get("language"), "confirm": request.headers.get("x-olivia-setup-action"),
                          "at": time.monotonic()})
            if on_language is not None:
                return on_language(route, body)
            return route.fulfill(content_type="application/json", body=json.dumps({"status": "SAVED", "language": body["language"], "persisted": True}))
        route.fulfill(content_type="application/json", body=json.dumps(harness._reply(path, body, state)))

    page.route(ORIGIN + "/**", handle)
    page.goto(ORIGIN + "/#/settings")
    page.wait_for_selector("[data-olivia-group=language]")
    page.wait_for_timeout(900)
    return page


@pytest.fixture(scope="module")
def playwright_instance():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as p:
        yield p


@pytest.fixture()
def browser(playwright_instance):
    browser = harness._launch(playwright_instance)
    yield browser
    browser.close()


def _open_switch(page):
    page.evaluate("document.querySelector('[data-olivia-group=language]').open = true")
    return page.locator("[data-olivia-group=language]")


# --- the report -------------------------------------------------------------------------------------------------

def test_the_page_reports_chinese_at_start_when_nothing_was_chosen(browser):
    page = _boot(browser)
    assert [post["language"] for post in page.posts] == ["zh-cn"]
    assert page.posts[0]["confirm"] == "confirmed"
    assert page.errors == []


def test_the_page_reports_the_stored_english_choice_at_start(browser):
    page = _boot(browser, "en")
    assert [post["language"] for post in page.posts] == ["en"]
    assert page.errors == []


def test_switching_reports_the_new_language_before_the_page_reloads(browser):
    held = []
    page = _boot(browser, on_language=lambda route, body: held.append((route, body)))
    # the start-up report is still unanswered; answer it so that only the switch is pending below
    for route, body in held:
        route.fulfill(content_type="application/json", body=json.dumps({"status": "SAVED", "language": body["language"]}))
    held.clear()
    page.evaluate("window.__beforeSwitch = true")
    group = _open_switch(page)
    group.get_by_role("radio", name="English").click()
    page.wait_for_timeout(500)
    # the choice is stored at once, the report is sent and the page waits for its answer
    assert page.evaluate("localStorage.getItem('olivia.ui.language')") == "en"
    assert [post["language"] for post in page.posts][-1] == "en"
    assert page.evaluate("window.__beforeSwitch") is True
    held[-1][0].fulfill(content_type="application/json", body=json.dumps({"status": "SAVED", "language": "en", "persisted": True}))
    page.get_by_text("How she replies").wait_for()
    assert page.evaluate("window.__beforeSwitch") is None
    assert [post["language"] for post in page.posts] == ["zh-cn", "en", "en"]  # start (Chinese), switch, start after the reload
    assert page.errors == []


def test_the_switch_does_not_wait_for_ever_for_a_silent_backend(browser):
    held = []
    page = _boot(browser, on_language=lambda route, body: held.append(route))
    page.evaluate("window.__beforeSwitch = true")
    group = _open_switch(page)
    started = time.monotonic()
    group.get_by_role("radio", name="English").click()
    page.get_by_text("How she replies").wait_for(timeout=8000)
    assert 2.0 <= time.monotonic() - started < 6.0  # the reload follows after the short cap
    assert page.errors == []


def test_the_switch_still_works_when_the_report_fails(browser):
    page = _boot(browser, on_language=lambda route, body: route.fulfill(status=500, content_type="application/json", body="{}"))
    group = _open_switch(page)
    group.get_by_role("radio", name="English").click()
    page.get_by_text("How she replies").wait_for()
    assert page.evaluate("localStorage.getItem('olivia.ui.language')") == "en"
    assert page.errors == []


def test_switching_back_reports_chinese(browser):
    page = _boot(browser)
    page.evaluate("localStorage.setItem('olivia.ui.language', 'en')")
    page.reload()
    page.get_by_text("How she replies").wait_for()
    page.wait_for_timeout(500)
    group = _open_switch(page)
    group.get_by_role("radio", name="中文").click()
    page.get_by_text("林离怎么回复").wait_for()
    assert [post["language"] for post in page.posts] == ["zh-cn", "en", "zh-cn", "zh-cn"]


# --- pages that show backend text --------------------------------------------------------------------------------

def _sticker_packs(tmp_path, monkeypatch):
    from runtime.letter_stickers import packs

    ids = {f"linli-{i:03d}": tmp_path / f"{i}.png" for i in range(109, 273)}
    monkeypatch.setattr(packs, "installed", lambda data_root: ids)
    return packs.status(tmp_path)


@pytest.mark.parametrize("language,expected", [("en", "Sticker packs: line art (built in), Hand-drawn, Quiet blue, Vinyl, Dramatic manga, Whimsical manga, Animated"),
                                               ("zh-cn", "表情包：线稿（自带）、手绘、静蓝、唱片、戏剧漫画、奇想漫画、动图")])
def test_the_sticker_pack_row_shows_the_names_the_backend_sends(browser, tmp_path, monkeypatch, language, expected):
    ui_language.set_language(language)
    packs = _sticker_packs(tmp_path, monkeypatch)
    page = browser.new_page(viewport={"width": 1100, "height": 1000}, locale="en-US")
    page.add_init_script(f"try{{localStorage.setItem('olivia.ui.language','{language}')}}catch(_){{}}")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def handle(route):
        request = route.request
        path = request.url[len(ORIGIN):].split("?")[0]
        if path == "/assets/olivia-companion-settings.js":
            return route.fulfill(content_type="application/javascript", body=BOOTSTRAP_JAVASCRIPT)
        if path == "/":
            return route.fulfill(content_type="text/html", body=harness.PAGE % {"origin": ORIGIN, "chat": ""})
        body = request.post_data_json if request.method == "POST" else None
        if path == "/toy/sticker-packs":
            return route.fulfill(content_type="application/json", body=json.dumps({"code": 0, "data": {"folder": "C:\\Olivia\\stickers", "packs": packs}}))
        route.fulfill(content_type="application/json", body=json.dumps(harness._reply(path, body, harness.CHAT_STATES["choice"])))

    page.route(ORIGIN + "/**", handle)
    page.goto(ORIGIN + "/#/settings")
    page.wait_for_selector("[data-olivia-group=language]")
    page.evaluate("document.querySelectorAll('[data-olivia-group]').forEach(group => { group.open = true })")
    row = page.get_by_text(expected, exact=True)
    row.wait_for(state="attached")
    if language == "en":
        assert not HAN.search(row.text_content())
    assert errors == []


def test_the_relationship_panel_reads_well_with_the_wording_the_backend_sends(browser, tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from runtime.private_world.ledger import SQLitePrivateWorldLedger
    from runtime.relationship_lite import details
    from runtime.relationship_moments import record_moment

    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    target = SQLitePrivateWorldLedger(tmp_path / "world.sqlite3")
    with target._connection() as db:
        record_moment(db, "source", now.isoformat(), {"moment": "rel_plan:created", "user_quote": "Sunny day", "reply_quote": "Yes"})
    server = SimpleNamespace(private_world_relationship_committer=SimpleNamespace(ledger=target), daily_life_runtime=None)
    ui_language.set_language("en")
    data = details(server, now)
    reader = BOOTSTRAP_JAVASCRIPT.split("  const requestJson = ", 1)[1].split("  const publishProactiveState", 1)[0]
    renderer = BOOTSTRAP_JAVASCRIPT.split("    const relationshipPanel = ", 1)[1].split("    const relationship = relationshipPanel();", 1)[0]
    body = """
      const alive = () => true;
      const text = (tag, value, cls = '') => { const e = document.createElement(tag); e.textContent = value; e.className = cls; return e };
      const button = (label, handler) => { const e = text('button', label); e.onclick = handler; return e };
      const apiBase = 'http://relationship.test', CONFIRM_HEADER = 'x-c', CONFIRM_VALUE = '1';
      const VIDEO_CAPABILITY_PATH = 'vc', VIDEO_REPLY_SETTINGS_PATH = 'vs', MEMORY_PATH = 'm', DAILY_LIFE_PATH = 'd', STATUS_PATH = 's', PROACTIVE_STATUS_PATH = 'p', LOCAL_LETTER_IMPORT_PATH = 'l';
      const requestJson = """ + reader + "\nwindow.makePanel = " + renderer
    page = browser.new_page(viewport={"width": 860, "height": 1000})
    page.add_init_script("try{localStorage.setItem('olivia.ui.language','en')}catch(_){}")
    page.route("http://relationship.test/", lambda r: r.fulfill(content_type="text/html", body="<main id='panel'></main>"))
    page.route("http://relationship.test/toy/relationship**", lambda r: r.fulfill(content_type="application/json", body=json.dumps({"code": 0, "data": data})))
    page.goto("http://relationship.test/")
    page.add_script_tag(content=I18N_JAVASCRIPT)
    page.add_script_tag(content="window.t = window.oliviaI18n.t;" + body)
    page.evaluate("async () => { const panel = window.makePanel(); document.querySelector('#panel').replaceChildren(panel); panel.open = true; await panel.refresh() }")
    page.get_by_text("Not defined yet · Volume").wait_for()
    shown = page.inner_text("#panel")
    assert not HAN.search(shown), shown
    for line in (data["title"], data["description"], data["dynamic"], data["hint"], data["dimensions"]["trust"]["text"]):
        assert line in shown, line
    # long sentences wrap inside the panel instead of running out of it
    assert page.evaluate("document.querySelector('#panel').scrollWidth <= document.querySelector('#panel').clientWidth + 1")
    assert page.evaluate("[...document.querySelectorAll('dt')].every(el => el.getBoundingClientRect().height < 30)")
