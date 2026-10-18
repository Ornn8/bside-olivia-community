"""The letters-and-memories window in a browser, with synthetic data only.

The top-bar entry opens one window with three tabs: letters (the mailbox list with deletion, plus a search of the
original text), memories (the memory browser and its management under 更多) and recently deleted letters. The whole
injected settings script runs against ``letters_memories_fixture.Service``, an in-process stand-in for the local
service; nothing here is a real letter, memory or account.
"""

from __future__ import annotations

import pytest

from tests.installer.letters_memories_fixture import Service, Unavailable, boot

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect
sync_playwright = sync_api.sync_playwright

LETTER_ROWS = ".lm-list:not([hidden]) .lm-row"
MEMORY_PATH = "/toy/companion/memory"


def _launch(playwright):
    for options in ({"channel": "chrome"}, {"channel": "msedge"}, {}):
        try:
            return playwright.chromium.launch(headless=True, **options)
        except Exception:  # noqa: BLE001 - try the next browser
            continue
    pytest.skip("No Chromium-based browser available")


@pytest.fixture()
def browser():
    with sync_playwright() as playwright:
        instance = _launch(playwright)
        yield instance
        instance.close()


def _open(page, expected_rows=6, *, narrow=False):
    page.wait_for_selector("[data-letter-deletion-entry]")
    if narrow:
        # the fixed top bar runs past a phone-width window, so its button is pressed by script there
        page.evaluate("document.querySelector('[data-letter-deletion-entry]').click()")
    else:
        page.click("[data-letter-deletion-entry]")
    window = page.locator("[data-letter-deletion]")
    window.wait_for()
    expect(window.locator(LETTER_ROWS)).to_have_count(expected_rows)
    return window


def _tab(window, name):
    return window.get_by_role("tab", name=name, exact=True)


def _memory_tab(window):
    _tab(window, "记忆").click()
    expect(window.locator(".om-row")).to_have_count(6)


def _confirm(page):
    prompt = page.locator("[data-letter-confirm]")
    expect(prompt).to_be_visible()
    return prompt


def test_top_bar_has_one_entry_that_opens_a_window_with_three_tabs(browser):
    service = Service("zh")
    page = boot(browser, service)
    page.wait_for_selector("[data-letter-deletion-entry]")
    entry = page.locator("[data-letter-deletion-entry]")
    expect(entry).to_have_text("信件与记忆")
    assert page.locator("[data-olivia-main-navigation]").get_by_text("管理信件").count() == 0
    assert page.locator("[data-letter-deletion-entry]").count() == 1
    entry.click()
    window = page.locator("[data-letter-deletion]")
    expect(window).to_have_attribute("aria-label", "信件与记忆")
    expect(window.get_by_role("heading", name="信件与记忆")).to_be_visible()
    tabs = window.get_by_role("tab")
    expect(tabs).to_have_text(["信件", "记忆", "最近删除"])
    expect(_tab(window, "信件")).to_have_attribute("aria-selected", "true")
    expect(window.locator(LETTER_ROWS)).to_have_count(6)
    # a second click does not open a second window
    page.evaluate("document.querySelector('[data-letter-deletion-entry]').click()")
    assert page.locator("[data-letter-deletion]").count() == 1
    assert page.errors == []


def test_letter_tab_keeps_expanding_ticking_and_deleting_with_its_confirmations(browser):
    service = Service("zh")
    page = boot(browser, service)
    window = _open(page)
    window.locator(".lm-list > div").nth(0).get_by_role("button").click()
    boxes = window.locator("input[type=checkbox]")
    expect(boxes).to_have_count(2)
    boxes.nth(0).check()
    expect(window.locator(".lm-count")).to_have_text("已选 1 封")
    window.get_by_role("button", name="删除所选").click()
    prompt = _confirm(page)
    expect(prompt).to_contain_text("她关于这封信的记忆也会一并清理")
    prompt.get_by_role("button", name="取消").click()
    assert service.calls("/toy/letter/deletion/delete") == []
    window.get_by_role("button", name="删除所选").click()
    _confirm(page).get_by_role("button", name="确定").click()
    expect(window.locator(".lm-notice")).to_contain_text("已删除 1 封")
    assert [body for _, body in service.calls("/toy/letter/deletion/delete")] == [{"letter_id": "synthetic-0", "part": "user"}]
    assert page.errors == []


def test_recently_deleted_tab_restores_after_a_confirmation(browser):
    service = Service("zh")
    page = boot(browser, service)
    window = _open(page)
    _tab(window, "最近删除").click()
    expect(window.locator(LETTER_ROWS)).to_have_count(1)
    expect(window.locator(".lm-search")).to_be_hidden()
    expect(window.locator(".lm-notice")).to_contain_text("保留 30 天")
    window.get_by_role("button", name="恢复", exact=True).click()
    prompt = _confirm(page)
    expect(prompt).to_contain_text("会一起恢复")
    prompt.get_by_role("button", name="取消").click()
    assert service.calls("/toy/letter/deletion/restore") == []
    window.get_by_role("button", name="恢复", exact=True).click()
    _confirm(page).get_by_role("button", name="确定").click()
    expect(window.locator(".lm-notice")).to_contain_text("信件和关联记忆已恢复")
    assert [body for _, body in service.calls("/toy/letter/deletion/restore")] == [{"id": "trash-1"}]
    # back to the letters: the list is the mailbox again, with its search
    _tab(window, "信件").click()
    expect(window.locator(".lm-search")).to_be_visible()
    expect(window.locator(LETTER_ROWS)).to_have_count(6)
    assert page.errors == []


def test_letter_search_reads_the_original_text_and_the_full_text(browser):
    service = Service("zh")
    page = boot(browser, service)
    window = _open(page)
    # a tick made before searching is still there after the search is cleared
    window.locator(".lm-list > div").nth(0).get_by_role("button").click()
    window.locator("input[type=checkbox]").nth(0).check()
    search = window.locator(".lm-search input")
    expect(search).to_have_attribute("placeholder", "搜索全部信件原文")
    search.fill("雨")
    expect(window.locator(".lm-count")).to_have_text("找到 2 条匹配结果")
    expect(window.locator(LETTER_ROWS)).to_have_count(2)
    expect(window.get_by_role("button", name="删除所选")).to_be_hidden()
    expect(window.locator(".lm-notice")).to_contain_text("搜索结果仅供阅读")
    assert service.calls(MEMORY_PATH)[-1][0]["query"] == "雨"
    # the first result is an excerpt of a long letter; its full text can be read
    preview = window.locator(".lm-preview")
    expect(preview).to_contain_text("我的信")
    expect(preview).to_contain_text("当前为原文节选。")
    excerpt = preview.inner_text()
    window.get_by_role("button", name="读取完整原文").click()
    expect(preview).not_to_contain_text("当前为原文节选。")
    assert len(preview.inner_text()) > len(excerpt)
    assert service.calls(MEMORY_PATH)[-1][0]["full"] == "1"
    # the second result is her reply
    window.locator(LETTER_ROWS).nth(1).get_by_role("button").click()
    expect(preview).to_contain_text("她的回信")
    expect(preview).to_contain_text("雨停之后的空气总是很干净")
    # nothing found
    search.fill(service.copy["empty_query"])
    expect(window.locator(".lm-list:not([hidden])")).to_contain_text("没有匹配的信件")
    # clearing the search is the mailbox list again, with the tick and the deletion button
    search.fill("")
    expect(window.locator(LETTER_ROWS)).to_have_count(7)
    expect(window.get_by_role("button", name="删除所选")).to_be_visible()
    expect(window.locator(".lm-count")).to_have_text("已选 1 封")
    expect(window.locator("input[type=checkbox]").nth(0)).to_be_checked()
    assert page.errors == []


def test_letter_search_pages_through_many_results(browser):
    service = Service("zh", repeat=4)
    page = boot(browser, service)
    page.wait_for_selector("[data-letter-deletion-entry]")
    page.click("[data-letter-deletion-entry]")
    window = page.locator("[data-letter-deletion]")
    window.locator(".lm-search input").fill("。")
    expect(window.locator(".lm-footer")).to_contain_text("第 1 / 3 页")
    expect(window.locator(LETTER_ROWS)).to_have_count(20)
    expect(window.get_by_role("button", name="上一页")).to_be_disabled()
    window.get_by_role("button", name="下一页").click()
    expect(window.locator(".lm-footer")).to_contain_text("第 2 / 3 页")
    assert service.calls(MEMORY_PATH)[-1][0]["page"] == "2"
    window.get_by_role("button", name="下一页").click()
    expect(window.get_by_role("button", name="下一页")).to_be_disabled()
    window.get_by_role("button", name="上一页").click()
    expect(window.locator(".lm-footer")).to_contain_text("第 2 / 3 页")
    assert page.errors == []


def test_search_failure_is_reported_and_leaves_the_list_alone(browser):
    service = Service("zh")
    original = service.answer

    def answer(method, path, query, body):
        if path == MEMORY_PATH and query.get("collection") == "originals":
            service.requests.append((path, query, body))
            raise Unavailable()
        return original(method, path, query, body)

    service.answer = answer
    page = boot(browser, service)
    window = _open(page)
    window.locator(".lm-search input").fill("雨")
    expect(window.locator(".lm-list:not([hidden])")).to_contain_text("搜索暂时不可用")
    window.locator(".lm-search input").fill("")
    expect(window.locator(LETTER_ROWS)).to_have_count(6)
    assert page.errors == []


def test_memory_tab_browses_and_hands_a_memory_over_to_its_source_letter(browser):
    service = Service("zh")
    page = boot(browser, service)
    window = _open(page)
    _memory_tab(window)
    expect(window.locator(".om-heading")).to_contain_text("状态：可用 · 6 条记忆")
    rows = window.locator(".om-row")
    search = window.locator(".om-search input")
    search.fill("冰淇淋")
    expect(rows).to_have_count(1)
    search.fill("")
    expect(rows).to_have_count(6)
    assert {query["collection"] for query, _ in service.calls(MEMORY_PATH)} == {"memories"}
    rows.nth(1).click()
    expect(window.locator(".om-detail h3")).to_contain_text("十六小节")
    window.get_by_role("button", name="查看关联原文").click()
    # the letter tab shows the letter this memory came from
    expect(_tab(window, "信件")).to_have_attribute("aria-selected", "true")
    expect(window.locator(".lm-source")).to_be_visible()
    expect(window.locator(".lm-source")).to_contain_text("正在查看关联原文")
    expect(window.locator(LETTER_ROWS)).to_have_count(2)
    expect(window.locator(".lm-count")).to_have_text("共 2 条原文")
    assert service.calls(MEMORY_PATH)[-1][0]["source_id"] == "letter-1"
    expect(window.locator(".lm-preview")).to_contain_text(service.copy["letters"][1][1][:12])
    window.locator(LETTER_ROWS).nth(1).get_by_role("button").click()
    expect(window.locator(".lm-preview")).to_contain_text("她的回信")
    # a new search leaves the linked letter; 显示全部信件 returns to the mailbox list
    window.get_by_role("button", name="显示全部信件").click()
    expect(window.locator(".lm-source")).to_be_hidden()
    expect(window.locator(LETTER_ROWS)).to_have_count(6)
    expect(window.get_by_role("button", name="删除所选")).to_be_visible()
    # the memory tab kept the record that was open
    _tab(window, "记忆").click()
    expect(window.locator(".om-row").nth(1)).to_have_attribute("aria-current", "true")
    assert page.errors == []


def test_management_lives_under_more_and_keeps_its_confirmations(browser):
    service = Service("zh")
    page = boot(browser, service)
    window = _open(page)
    _memory_tab(window)
    management = window.locator(".om-management")
    expect(management).to_be_hidden()
    window.locator(".om-heading button").click()
    expect(management).to_be_visible()
    expect(management).to_contain_text("已接入 12 封信")
    expect(management).to_contain_text("历史信件 20/24 封")
    for name in ("暂停长期记忆", "清空当前用户记忆", "重试未写入的记忆"):
        expect(management.get_by_role("button", name=name)).to_be_visible()
    # clearing needs two confirmations; cancelling the first stops it
    management.get_by_role("button", name="清空当前用户记忆").click()
    prompt = _confirm(page)
    expect(prompt).to_contain_text("确认清空当前用户的 Mem0 长期记忆？")
    prompt.get_by_role("button", name="取消").click()
    assert service.calls("/toy/companion/memory/clear") == []
    management.get_by_role("button", name="清空当前用户记忆").click()
    _confirm(page).get_by_role("button", name="确定").click()
    expect(_confirm(page)).to_contain_text("清空后无法恢复")
    _confirm(page).get_by_role("button", name="确定").click()
    expect(window.locator(".om-row")).to_have_count(6)
    (_, body), = service.calls("/toy/companion/memory/clear")
    assert body["confirmed"] is True and body["request_id"].startswith("memory.clear.")
    # pausing asks once
    window.locator(".om-heading button").click()
    window.get_by_role("button", name="暂停长期记忆").click()
    _confirm(page).get_by_role("button", name="确定").click()
    expect(window.locator(".om-row")).to_have_count(6)
    assert len(service.calls("/toy/companion/memory/pause")) == 1
    assert page.errors == []


def test_deleting_one_memory_asks_in_the_window(browser):
    service = Service("zh")
    page = boot(browser, service)
    window = _open(page)
    _memory_tab(window)
    window.locator(".olivia-memory-more summary").click()
    window.locator(".olivia-memory-more > button").click()
    prompt = _confirm(page)
    expect(prompt).to_contain_text("确认删除这条长期记忆？原始信件不会被删除。")
    prompt.get_by_role("button", name="确定").click()
    expect(window.locator(".om-result")).to_have_text("长期记忆已删除。")
    (_, body), = service.calls("/toy/companion/memory/delete")
    assert body["memory_id"] == "memory-0"
    assert page.errors == []


def test_escape_closes_one_layer_at_a_time(browser):
    service = Service("zh")
    page = boot(browser, service)
    window = _open(page)
    _memory_tab(window)
    window.locator(".om-heading button").click()
    window.get_by_role("button", name="清空当前用户记忆").click()
    _confirm(page)
    page.keyboard.press("Escape")
    expect(page.locator("[data-letter-confirm]")).to_have_count(0)
    expect(window).to_be_visible()
    expect(window.locator(".om-management")).to_be_visible()
    page.keyboard.press("Escape")
    expect(window.locator(".om-management")).to_be_hidden()
    expect(window).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("[data-letter-deletion]")).to_have_count(0)
    assert service.calls("/toy/companion/memory/clear") == []
    assert page.errors == []


@pytest.mark.parametrize("name,capability,button_name", [
    ("clear pending", {"state": "unavailable", "reason_code": "MEMORY_ADMIN_CLEAR_PENDING"}, "继续完成清空"),
    ("embedding missing", {"state": "unavailable", "reason_code": "MEM0_EMBEDDING_CACHE_UNAVAILABLE", "embedding": {"state": "missing"}}, "导入记忆离线包"),
    ("not prepared", {"state": "unavailable", "reason_code": "MEM0_INITIALIZATION_FAILED"}, "重新准备长期记忆"),
])
def test_memory_tab_offers_the_recovery_controls_when_memory_is_not_ready(browser, name, capability, button_name):
    service = Service("zh", memory_capability=capability)
    page = boot(browser, service)
    window = _open(page)
    _tab(window, "记忆").click()
    expect(window.get_by_role("button", name=button_name)).to_be_visible()
    expect(window.locator(".om-row")).to_have_count(0)
    # the letters are not affected by it
    _tab(window, "信件").click()
    expect(window.locator(LETTER_ROWS)).to_have_count(6)
    assert page.errors == []


def test_paused_memory_is_reported_in_the_tab_and_resumes_from_more(browser):
    service = Service("zh", memory_capability={"state": "available", "reason_code": "MEMORY_ADMIN_PAUSED", "count": 6})
    page = boot(browser, service)
    window = _open(page)
    _tab(window, "记忆").click()
    expect(window.locator(".om-heading")).to_contain_text("已暂停")
    expect(window.locator(".om-rows")).to_contain_text("长期记忆已暂停。恢复后可继续浏览。")
    window.locator(".om-heading button").click()
    expect(window.get_by_role("button", name="恢复长期记忆")).to_be_visible()
    assert page.errors == []


def test_settings_keeps_its_status_lines_and_opens_the_window_on_the_memory_tab(browser):
    service = Service("zh")
    page = boot(browser, service, hash="#/settings")
    page.wait_for_selector("[data-olivia-group=letters]")
    page.evaluate("document.querySelector('[data-olivia-group=letters]').open = true")
    page.get_by_role("button", name="打开", exact=True).first.click()
    settings = page.locator("[data-olivia-companion-settings-dialog]")
    panel = settings.locator("[data-olivia-companion-panel=memory]")
    expect(settings.get_by_role("heading", name="长期记忆", level=2)).to_be_visible()
    expect(panel).to_contain_text("状态：可用 · 6 条记忆")
    expect(panel.get_by_role("button")).to_have_text(["打开信件与记忆"])
    assert settings.locator("[data-olivia-memory-browser]").count() == 0
    assert not [call for call in service.calls(MEMORY_PATH) if call[0].get("browse")], "no records are read for the launcher"
    panel.get_by_role("button", name="打开信件与记忆").click()
    window = page.locator("[data-letter-deletion]")
    expect(window).to_be_visible()
    expect(_tab(window, "记忆")).to_have_attribute("aria-selected", "true")
    expect(window.locator(".om-row")).to_have_count(6)
    # closing the window returns to the settings dialog
    window.get_by_role("button", name="关闭").click()
    expect(page.locator("[data-letter-deletion]")).to_have_count(0)
    expect(settings).to_be_visible()
    assert page.errors == []


def test_settings_launcher_shows_a_paused_service(browser):
    service = Service("zh", memory_capability={"state": "available", "reason_code": "MEMORY_ADMIN_PAUSED", "count": 6})
    page = boot(browser, service, hash="#/settings")
    page.wait_for_selector("[data-olivia-group=letters]")
    page.evaluate("document.querySelector('[data-olivia-group=letters]').open = true")
    page.get_by_role("button", name="打开", exact=True).first.click()
    panel = page.locator("[data-olivia-companion-panel=memory]")
    expect(panel).to_contain_text("状态：已暂停")
    expect(panel.get_by_role("button", name="打开信件与记忆")).to_be_visible()
    assert page.errors == []


@pytest.mark.parametrize("width", [390, 360])
def test_narrow_window_has_no_horizontal_overflow_and_reads_one_pane_at_a_time(browser, width):
    service = Service("zh")
    page = boot(browser, service, width=width, height=780)
    window = _open(page, narrow=True)

    def fits(label):
        assert window.evaluate("(el) => el.scrollWidth <= el.clientWidth"), label
        box = window.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width, label

    fits("letters")
    window.locator(".lm-search input").fill("雨")
    expect(window.locator(".lm-count")).to_have_text("找到 2 条匹配结果")
    expect(window.get_by_role("button", name="下一页")).to_be_visible()
    fits("search")
    window.locator(".lm-search input").fill("")
    _tab(window, "最近删除").click()
    expect(window.locator(LETTER_ROWS)).to_have_count(1)
    fits("recent")
    _memory_tab(window)
    fits("memory")
    window.locator(".om-row").nth(0).click()
    expect(window.locator(".om-detail")).to_be_visible()
    expect(window.locator(".om-list-panel")).to_be_hidden()
    fits("memory detail")
    # on a narrow window Escape first returns from the record to the list, and keeps the window
    page.keyboard.press("Escape")
    expect(window.locator(".om-list-panel")).to_be_visible()
    expect(window).to_be_visible()
    window.locator(".om-heading button").click()
    expect(window.locator(".om-management")).to_be_visible()
    fits("management")
    box = window.locator(".om-management").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width
    assert page.errors == []


def test_memory_tab_fills_the_window_on_a_short_screen(browser):
    service = Service("zh")
    page = boot(browser, service, width=1000, height=640)
    window = _open(page)
    _memory_tab(window)
    window_box = window.bounding_box()
    browser_box = window.locator("[data-olivia-memory-browser]").bounding_box()
    assert browser_box["y"] + browser_box["height"] <= window_box["y"] + window_box["height"] + 1
    assert window.locator(".om-rows").evaluate("(el) => el.scrollHeight >= el.clientHeight")
    assert page.errors == []
