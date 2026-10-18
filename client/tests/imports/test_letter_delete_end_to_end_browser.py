"""The letters-and-memories window in a real browser, talking to the real local backend. Synthetic data only.

The window is the product's injected script (``BOOTSTRAP_JAVASCRIPT``) on a stand-in page; every request it makes goes
over HTTP to the real backend of ``letter_delete_e2e_backend.py`` (see ``test_letter_delete_end_to_end.py``). The test
opens 信件与记忆, visits the memory tab first (so that it is already loaded), deletes the user's letter from the letter
tab, and checks that the letter tab, the memory tab and the original-text search show the deletion. It then restores
the letter from 最近删除 and checks that everything is back. The state of every store is read from the backend at each
step.

Set ``OLIVIA_E2E_SHOTS`` to a folder to keep the screenshots (and ``evidence-browser.json``) there.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
from tests.imports.letter_delete_e2e_support import BY_KEY, Backend, comparable, counts, mem0_site, script_for, seed
from tests.installer.letters_memories_fixture import PAGE

sync_api = pytest.importorskip('playwright.sync_api')
expect = sync_api.expect
sync_playwright = sync_api.sync_playwright

SITE = mem0_site()
pytestmark = pytest.mark.skipif(SITE is None, reason='Mem0 and Qdrant are not installed (see OLIVIA_E2E_MEM0_SITE)')

LETTER_ROWS = '.lm-list:not([hidden]) .lm-row'


def _launch(playwright):
    for options in ({'channel': 'chrome'}, {'channel': 'msedge'}, {}):
        try:
            return playwright.chromium.launch(headless=True, **options)
        except Exception:  # noqa: BLE001 - try the next browser
            continue
    pytest.skip('No Chromium-based browser available')


def _boot(browser, backend):
    """The window's script on a stand-in page. The page comes from ``localhost`` and the local service is called on
    ``127.0.0.1``: two origins, as in the app, where the page is not the service."""
    static = backend.root / 'static'
    static.mkdir(exist_ok=True)
    (static / 'olivia-companion-settings.js').write_text(BOOTSTRAP_JAVASCRIPT, encoding='utf-8')
    host = f'http://localhost:{backend.port}/__e2e__/static'
    (static / 'index.html').write_text(
        (PAGE % {'origin': host, 'title': '信箱', 'note': '合成数据 · 真实本机服务（背景为测试外壳）'})
        .replace(f'src="{host}/assets/', f'src="{host}/').replace(f'data-api-base="{host}/"', f'data-api-base="{backend.api.base}/"'),
        encoding='utf-8')
    page = browser.new_page(viewport={'width': 1200, 'height': 900}, locale='zh-CN')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.errors = errors
    page.goto(host + '/index.html#/collection')
    return page


def _tab(window, name):
    return window.get_by_role('tab', name=name, exact=True)


def _confirm(page):
    prompt = page.locator('[data-letter-confirm]')
    expect(prompt).to_be_visible()
    return prompt


def _group(window, exchange, side='user'):
    """The mailbox entry of an exchange: its title is the start of the user's letter, or of her reply once the
    user's letter is deleted."""
    return window.locator('.lm-list > div').filter(has_text=exchange[side][:9])


def test_the_window_follows_a_real_deletion_and_restore(tmp_path):
    shots = Path(os.environ.get('OLIVIA_E2E_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    exchange = BY_KEY['garden-done']
    backend = Backend(tmp_path / 'app', script_for(), SITE)
    try:
        backend.wait_ready()
        api = backend.api
        ids = seed(api)
        api.diary(add=True, text=exchange['user'])  # her diary of that day quotes the letter: the confirmation offers the choice
        api.settle()
        before = api.snapshot()
        record = {'stores': {'before': counts(before)}, 'ui': {}}
        with sync_playwright() as playwright:
            browser = _launch(playwright)
            try:
                page = _boot(browser, backend)
                page.wait_for_selector('[data-letter-deletion-entry]')
                page.click('[data-letter-deletion-entry]')
                window = page.locator('[data-letter-deletion]')
                window.wait_for()
                expect(window.locator(LETTER_ROWS)).to_have_count(5)

                # BEFORE: the five letters; the memory tab is opened once, so that it holds its list while the
                # letter is deleted from the other tab
                _group(window, exchange).get_by_role('button').first.click()
                expect(_group(window, exchange).locator('.lm-row')).to_have_count(2)
                page.screenshot(path=str(shots / '01-before-letters.png'))
                _tab(window, '记忆').click()
                expect(window.locator('.om-row')).to_have_count(5)
                expect(window.locator('.om-heading')).to_contain_text('5 条记忆')
                page.screenshot(path=str(shots / '02-before-memories.png'))
                window.locator('.om-search input').fill(exchange['markers']['user'][1])
                expect(window.locator('.om-row')).to_have_count(1)
                window.locator('.om-search input').fill('')
                expect(window.locator('.om-row')).to_have_count(5)
                _tab(window, '信件').click()
                window.locator('.lm-search input').fill(exchange['markers']['user'][1])
                expect(window.locator('.lm-count')).to_have_text('找到 1 条匹配结果')
                page.screenshot(path=str(shots / '03-before-search.png'))
                window.locator('.lm-search input').fill('')
                expect(window.locator(LETTER_ROWS)).to_have_count(6)  # five letters, one opened into its two sides

                # DELETE my letter (not her reply) through the window
                group = _group(window, exchange)
                group.get_by_role('button').first.click()
                group.locator('input[type=checkbox]').nth(0).check()
                expect(window.locator('.lm-count')).to_have_text('已选 1 封')
                page.screenshot(path=str(shots / '04-ticked-my-letter.png'))
                window.get_by_role('button', name='删除所选').click()
                # the confirmation offers to delete the diary of that day and write it again: unchecked, with the short line
                prompt = _confirm(page)
                expect(prompt.get_by_label('删除当天日记并重新生成')).not_to_be_checked()
                expect(prompt).to_contain_text('日记是她自己写的，不会因删信自动改动。')
                page.screenshot(path=str(shots / '04a-confirmation-with-diary-choice.png'))
                prompt.get_by_role('button', name='确定').click()
                expect(window.locator('.lm-notice')).to_contain_text('已删除 1 封')
                api.settle()
                after = api.snapshot()
                record['stores']['after_delete'] = counts(after)
                assert after['databases']['diary'] == before['databases']['diary'], 'her diary stays as written'

                # AFTER DELETE, letter tab: the letter is still there with only her reply
                expect(window.locator(LETTER_ROWS)).to_have_count(5)
                _group(window, exchange, 'reply').get_by_role('button').first.click()
                expect(_group(window, exchange, 'reply').locator('.lm-row')).to_have_count(1)
                expect(_group(window, exchange, 'reply')).to_contain_text('她的回信')
                expect(_group(window, exchange, 'reply')).not_to_contain_text('我的信')
                page.screenshot(path=str(shots / '05-after-delete-letters.png'))
                # memory tab (opened before the deletion): the memory made from the letter is gone
                _tab(window, '记忆').click()
                expect(window.locator('.om-row')).to_have_count(4)
                expect(window.locator('.om-heading')).to_contain_text('4 条记忆')
                expect(window.locator('.om-list-panel')).not_to_contain_text(exchange['markers']['user'][1])
                page.screenshot(path=str(shots / '06-after-delete-memories.png'))
                # search: my words are not found, her reply is
                _tab(window, '信件').click()
                window.locator('.lm-search input').fill(exchange['markers']['user'][1])
                expect(window.locator('.lm-list:not([hidden])')).to_contain_text('没有匹配的信件')
                page.screenshot(path=str(shots / '07-after-delete-search-mine-gone.png'))
                window.locator('.lm-search input').fill('多晒太阳')
                expect(window.locator('.lm-count')).to_have_text('找到 1 条匹配结果')
                expect(window.locator('.lm-preview')).to_contain_text('她的回信')
                page.screenshot(path=str(shots / '08-after-delete-search-hers-kept.png'))
                window.locator('.lm-search input').fill('')
                # recently deleted
                _tab(window, '最近删除').click()
                expect(window.locator(LETTER_ROWS)).to_have_count(1)
                expect(window.locator(LETTER_ROWS)).to_contain_text('我的信')
                page.screenshot(path=str(shots / '09-recently-deleted.png'))

                # RESTORE through the window
                window.get_by_role('button', name='恢复', exact=True).click()
                _confirm(page).get_by_role('button', name='确定').click()
                expect(window.locator('.lm-notice')).to_contain_text('信件和关联记忆已恢复')
                api.settle()
                restored = api.snapshot()
                record['stores']['after_restore'] = counts(restored)
                page.screenshot(path=str(shots / '10-restored.png'))
                _tab(window, '信件').click()
                expect(window.locator(LETTER_ROWS)).to_have_count(5)
                _group(window, exchange).get_by_role('button').first.click()
                expect(_group(window, exchange).locator('.lm-row')).to_have_count(2)
                page.screenshot(path=str(shots / '11-after-restore-letters.png'))
                _tab(window, '记忆').click()
                expect(window.locator('.om-row')).to_have_count(5)
                expect(window.locator('.om-heading')).to_contain_text('5 条记忆')
                page.screenshot(path=str(shots / '12-after-restore-memories.png'))
                _tab(window, '信件').click()
                window.locator('.lm-search input').fill(exchange['markers']['user'][1])
                expect(window.locator('.lm-count')).to_have_text('找到 1 条匹配结果')
                page.screenshot(path=str(shots / '13-after-restore-search.png'))
                assert page.errors == []
                record['ui'] = {
                    'letters_tab_rows': 5, 'memory_tab_rows': {'before': 5, 'after_delete': 4, 'after_restore': 5},
                    'search_for_my_words': {'before': 1, 'after_delete': 0, 'after_restore': 1},
                    'search_for_her_reply_after_delete': 1, 'recently_deleted_rows_after_delete': 1,
                }
            finally:
                browser.close()
        assert comparable(restored) == comparable(before)
        assert backend.blocked_connections == []
        record['ui']['diary_choice'] = 'offered, unchecked by default; her diary unchanged after the deletion'
        (shots / 'evidence-browser.json').write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True),
                                                     encoding='utf-8')
    finally:
        backend.stop()


def test_ticking_the_diary_choice_deletes_that_day_and_it_is_written_again(tmp_path):
    shots = Path(os.environ.get('OLIVIA_E2E_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    exchange = BY_KEY['garden-done']
    backend = Backend(tmp_path / 'app', script_for(), SITE)
    try:
        backend.wait_ready()
        api = backend.api
        seed(api)
        api.diary(add=True, text=exchange['user'])
        before = settled_snapshot(api)
        record = {'before': counts(before)}
        with sync_playwright() as playwright:
            browser = _launch(playwright)
            try:
                page = _boot(browser, backend)
                page.wait_for_selector('[data-letter-deletion-entry]')
                page.click('[data-letter-deletion-entry]')
                window = page.locator('[data-letter-deletion]')
                window.wait_for()
                group = _group(window, exchange)
                group.get_by_role('button').first.click()
                group.locator('input[type=checkbox]').nth(0).check()
                window.get_by_role('button', name='删除所选').click()
                prompt = _confirm(page)
                prompt.get_by_label('删除当天日记并重新生成').check()
                page.screenshot(path=str(shots / '14-confirmation-diary-choice-ticked.png'))
                prompt.get_by_role('button', name='确定').click()
                expect(window.locator('.lm-notice')).to_contain_text('已删除 1 封')
                after = settled_snapshot(api)
                record['after_delete'] = counts(after)
                assert counts(after)['diary'] == {'entries': 1, 'comments': 0, 'days_to_write_again': 1}
                # the diary writer then writes the day again without the deleted letter
                entries = api.diary_tick()
                (day,) = [d for d in entries if len(d) == 10]
                assert exchange['user'] not in entries[day] and BY_KEY['ice']['user'] in entries[day]
                record['after_written_again'] = counts(settled_snapshot(api))
                # restore from 最近删除: the original entry replaces the new one
                _tab(window, '最近删除').click()
                window.get_by_role('button', name='恢复', exact=True).click()
                _confirm(page).get_by_role('button', name='确定').click()
                expect(window.locator('.lm-notice')).to_contain_text('信件和关联记忆已恢复')
                restored = settled_snapshot(api)
                record['after_restore'] = counts(restored)
                assert api.diary_tick()[day] == [r['body'] for r in before['databases']['diary']['diary_entries'] if r['day'] == day][0]
                assert page.errors == []
            finally:
                browser.close()
        assert comparable(settled_snapshot(api)) == comparable(before)
        assert backend.blocked_connections == []
        (shots / 'evidence-browser-diary.json').write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True),
                                                           encoding='utf-8')
    finally:
        backend.stop()


def settled_snapshot(api):
    api.settle()
    return api.snapshot()
