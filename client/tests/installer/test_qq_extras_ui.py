"""The QQ settings group shows one switch per part of her QQ life; each saves on change and a failed save goes back."""
import asyncio
from pathlib import Path

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def mount(page, *, fail_saves=False, fail_load=False):
    source = BOOTSTRAP_JAVASCRIPT
    helpers = source[source.index('  const text = (tag, value, className)'):source.index('  const setButtonsBusy =')]
    component = source[source.index('  const mountQqExtras ='):source.index('  const goWorld =')]
    page.set_content('<style>body{background:#111214;color:#eee;font:16px sans-serif;margin:24px}</style><main id="app"></main>')
    page.add_script_tag(content=helpers + '''
      window.writes=[];const failSaves=%s,failLoad=%s;
      let saved={status:true,like:false,qzone:true,gestures:true};
      const routeRequest=async(path,body,options)=>{
        if(path!=='/toy/qq/extras')throw Error('path');
        if(body){if(!options.confirmed)throw Error('unconfirmed');window.writes.push(body);if(failSaves)throw Error('disk');saved=body;return saved;}
        if(failLoad)throw Error('offline');
        return saved;
      };
    ''' % ('true' if fail_saves else 'false', 'true' if fail_load else 'false')
        + component + "mountQqExtras(document.querySelector('#app'));")
    page.wait_for_timeout(150)


def test_each_switch_shows_its_saved_state_and_saves_the_whole_set():
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 900, 'height': 700})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        mount(page)
        switch = lambda name: page.get_by_role('switch', name=name, exact=True)
        assert switch('QQ 空间').is_checked() and not switch('名片点赞').is_checked()
        switch('名片点赞').click()
        page.wait_for_function('window.writes.length===1')
        assert page.evaluate('window.writes[0]') == {'qzone': True, 'status': True, 'like': True, 'gestures': True}
        assert switch('名片点赞').is_checked()
        output = Path('.evidence'); output.mkdir(exist_ok=True)
        page.screenshot(path=str(output / 'qq-extras.png'), full_page=True)
        page.set_viewport_size({'width': 400, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert not errors
        browser.close()


def test_a_failed_save_puts_the_switch_back_and_a_failed_read_leaves_them_locked():
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page()
        mount(page, fail_saves=True)
        page.get_by_role('switch', name='QQ 状态', exact=True).click()
        page.wait_for_timeout(150)
        assert page.get_by_role('switch', name='QQ 状态', exact=True).is_checked()
        assert '没有保存成功' in page.locator('body').inner_text()
        page = browser.new_page()
        mount(page, fail_load=True)
        assert page.get_by_role('switch', name='QQ 空间', exact=True).is_disabled()
        assert '暂时读取不到' in page.locator('body').inner_text()
        browser.close()


def test_the_local_route_reads_and_saves_the_switches(tmp_path, monkeypatch):
    import local_server
    monkeypatch.setattr(local_server, '_state_root', lambda: tmp_path)
    route = lambda method, body, confirmed=True: asyncio.run(
        local_server.route(method, '/toy/qq/extras', body, {}, companion_confirmed=confirmed))
    assert route('GET', {})['data'] == {'status': True, 'like': True, 'qzone': True, 'gestures': True}
    body = {'status': False, 'like': True, 'qzone': False, 'gestures': True}
    assert route('POST', body, confirmed=False)['message'] == 'COMPANION_CONFIRMATION_REQUIRED'
    assert route('POST', body)['data'] == body and route('GET', {})['data'] == body
    assert route('POST', {'status': 'no'})['message'] == 'QQ_EXTRAS_INVALID'
