"""Render the letters-and-memories window with synthetic data, without an app, a profile or an account.

Run from client: python scripts/preview_letters_memories.py --output <directory>
It runs the real injected script (the top-bar entry, the three tabs, the settings launcher) against an in-process
stand-in for the local service (tests/installer/letters_memories_fixture.py) and saves PNG screenshots.
Requires Playwright and its Chromium, or --channel msedge / chrome for an installed browser.
"""
import argparse
from pathlib import Path
import sys

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.installer.letters_memories_fixture import Service, boot  # noqa: E402

ROWS = '.lm-list:not([hidden]) .lm-row'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--channel')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    def save(page, name, **options):
        page.screenshot(path=str(args.output / (name + '.png')), **options)

    def open_window(page, narrow=False):
        page.wait_for_selector('[data-letter-deletion-entry]')
        if narrow:  # the fixed top bar runs past a phone-width window, so its button is pressed by script there
            page.evaluate("document.querySelector('[data-letter-deletion-entry]').click()")
        else:
            page.click('[data-letter-deletion-entry]')
        window = page.locator('[data-letter-deletion]')
        expect(window.locator(ROWS)).to_have_count(6)
        return window

    with sync_playwright() as p:
        browser = None
        for channel in ([args.channel] if args.channel else ['chrome', 'msedge', None]):
            try:
                browser = p.chromium.launch(channel=channel)
                break
            except Exception:  # noqa: BLE001 - try the next browser
                continue
        if browser is None:
            raise SystemExit('No Chromium-based browser available (python -m playwright install chromium)')

        # Chinese, desktop
        page = boot(browser, Service('zh'), width=1200, height=900)
        page.wait_for_selector('[data-letter-deletion-entry]')
        page.wait_for_timeout(900)
        save(page, '01-top-bar', clip={'x': 0, 'y': 0, 'width': 760, 'height': 130})
        window = open_window(page)
        window.locator('.lm-list > div').nth(0).get_by_role('button').click()
        window.locator('input[type=checkbox]').nth(0).check()
        expect(window.locator('.lm-count')).to_have_text('已选 1 封')
        window.locator('.lm-list > div').nth(0).get_by_role('button').nth(1).click()  # her reply into the preview
        save(page, '02-letters-tab')
        window.locator('.lm-search input').fill('雨')
        expect(window.locator('.lm-count')).to_have_text('找到 2 条匹配结果')
        save(page, '03-letters-search-results')
        window.locator('.lm-search input').fill('')
        expect(window.locator(ROWS)).to_have_count(7)
        window.get_by_role('tab', name='记忆', exact=True).click()
        expect(window.locator('.om-row')).to_have_count(6)
        save(page, '04-memory-tab')
        window.locator('.om-heading button').click()
        expect(window.locator('.om-management')).to_contain_text('历史信件 20/24 封')
        save(page, '05-memory-more-management')
        window.locator('.om-heading button').click()
        window.locator('.om-row').nth(1).click()
        window.get_by_role('button', name='查看关联原文').click()
        expect(window.locator('.lm-source')).to_be_visible()
        save(page, '06-memory-linked-letter')
        window.get_by_role('tab', name='最近删除', exact=True).click()
        expect(window.locator(ROWS)).to_have_count(1)
        save(page, '07-recently-deleted-tab')
        assert not page.errors, page.errors
        page.close()

        # English
        page = boot(browser, Service('en'), language='en', width=1200, height=900)
        page.wait_for_selector('[data-letter-deletion-entry]')
        page.wait_for_timeout(900)
        window = open_window(page)
        save(page, '08-letters-tab-english')
        assert not page.errors, page.errors
        page.close()

        # narrow
        page = boot(browser, Service('zh'), width=390, height=780)
        window = open_window(page, narrow=True)
        assert window.evaluate('(el) => el.scrollWidth <= el.clientWidth')
        save(page, '09-narrow-letters-tab')
        window.get_by_role('tab', name='记忆', exact=True).click()
        expect(window.locator('.om-row')).to_have_count(6)
        assert window.evaluate('(el) => el.scrollWidth <= el.clientWidth')
        save(page, '10-narrow-memory-tab')
        assert not page.errors, page.errors
        page.close()

        # settings: the memory page keeps its status lines and has one button
        page = boot(browser, Service('zh'), hash='#/settings', width=1200, height=900)
        page.wait_for_selector('[data-olivia-group=letters]')
        page.evaluate("document.querySelector('[data-olivia-group=letters]').open = true")
        page.get_by_role('button', name='打开', exact=True).first.click()
        expect(page.locator('[data-olivia-companion-panel=memory]')).to_contain_text('状态：可用 · 6 条记忆')
        save(page, '11-settings-memory-page')
        assert not page.errors, page.errors
        browser.close()
    print('Eleven synthetic screenshots saved; the three tabs, search, hand-over, management, narrow layout and the settings launcher worked.')


if __name__ == '__main__':
    main()
