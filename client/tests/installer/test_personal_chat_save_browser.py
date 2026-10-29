"""The QQ binding's second step (你的 QQ) in a browser: a saved number is shown, not asked again; another one can be
entered; saving reports what actually happened."""
import pytest

from runtime.personal_chat.setup_ui import PERSONAL_CHAT_SETUP_JAVASCRIPT

PAGE = '<style>body{background:#111;color:#eee;font:15px sans-serif;padding:25px}</style><main><div data-olivia-group-body="chat"></div></main>'
MOUNT = '(js) => {const s=document.createElement("script");s.dataset.apiBase="http://127.0.0.1:8899";s.textContent=js;document.body.append(s)}'


@pytest.mark.parametrize('case', ['connected', 'saved-not-connected', 'new'])
def test_saved_qq_is_shown_and_another_can_be_entered_without_revealing_owner(case, tmp_path):
    pw = pytest.importorskip('playwright.sync_api')
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome')
        page = browser.new_page(viewport={'width': 1100, 'height': 900})
        page.set_default_timeout(3000)
        page.set_content(PAGE)
        page.evaluate('''(kind) => {
          window.requests = [];
          const saved = kind !== 'new';
          window.fetch = async (url, options) => {
            if (String(url).endsWith('/configure')) {
              window.requests.push(JSON.parse(options.body));
              return {ok:true, json:async () => ({status:'CONNECTING'})};
            }
            return {ok:true, json:async () => ({selected_channels:['qq'], configured:{qq:kind === 'connected'},
              listeners:{qq:kind === 'connected' ? 'CONNECTED' : 'SETUP_REQUIRED'},
              qq:saved ? {state:kind === 'connected' ? 'CONNECTED' : 'IDLE', owner_masked:'*****4321', can_reuse_owner:true} : {state:'IDLE'},
              napcat:{state:'ONEBOT_READY',installed:true}})};
          };
        }''', case)
        page.evaluate(MOUNT, PERSONAL_CHAT_SETUP_JAVASCRIPT)
        owner = page.get_by_label('你自己的 QQ 号')
        if case == 'new':
            owner.wait_for()
            save = page.get_by_role('button', name='保存', exact=True)
            assert save.is_disabled()
            owner.fill('987654321')
            save.click()
            page.wait_for_function('window.requests.length === 1')
            assert page.evaluate('window.requests[0]') == {'managed': True, 'owner': '987654321'}
        else:
            page.get_by_text('你的 QQ：*****4321').wait_for()
            assert not owner.is_visible()  # a saved number is not asked again
            if case == 'saved-not-connected':
                page.get_by_role('button', name='用这个号', exact=True).click()
                page.wait_for_function('window.requests.length === 1')
                assert page.evaluate('window.requests[0]') == {'managed': True, 'reuse_saved_owner': True}
            else:
                assert page.get_by_role('button', name='用这个号').count() == 0
                page.get_by_role('button', name='换一个号').click()
                owner.fill('987654321')
                page.get_by_role('button', name='保存', exact=True).click()
                page.wait_for_function('window.requests.length === 1')
                assert page.evaluate('window.requests[0]') == {'managed': True, 'owner': '987654321'}
        assert '123454321' not in page.content()
        assert '机器人' not in page.content() and 'OneBot 已就绪' not in page.inner_text('body')
        page.screenshot(path=str(tmp_path / f'qq-owner-{case}.png'), full_page=True)
        browser.close()


def test_scan_that_logs_qq_back_in_replaces_the_qr_with_the_connected_view(tmp_path):
    # 2.5.7: once a re-scan logged QQ back in, the logged-in view threw "state is not defined"; the open page kept the
    # QR and 「QQ 已掉线，需要重新扫码登录」 although QQ was connected, and reopening it showed only the error.
    pw = pytest.importorskip('playwright.sync_api')
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome')
        page = browser.new_page(viewport={'width': 1100, 'height': 900})
        page.set_default_timeout(6000)
        errors = []
        page.on('pageerror', lambda exc: errors.append(str(exc)))
        page.set_content('<main><div data-olivia-group-body="chat"></div></main>')
        page.evaluate('''() => {
          const saved = {state:'IDLE', owner_masked:'*****4321', can_reuse_owner:true};
          window.loggedIn = false;
          window.fetch = async (url) => {
            if (String(url).endsWith('/napcat/login'))
              return {ok:true, json:async () => ({logged_in:false, scanned:false, verification_required:false,
                qr_data:'data:image/svg+xml;base64,PHN2Zy8+'})};
            return {ok:true, json:async () => ({selected_channels:['qq'], configured:{qq:true}, qq:saved,
              listeners:{qq:window.loggedIn ? 'CONNECTED' : 'RECONNECTING'},
              napcat:{state:window.loggedIn ? 'ONEBOT_READY' : 'AWAITING_QQ_LOGIN', installed:true, version:'v4.18.28'}})};
          };
        }''')
        page.evaluate('(js) => {const s=document.createElement("script");s.dataset.apiBase="http://127.0.0.1:8899";s.textContent=js;document.body.append(s)}', PERSONAL_CHAT_SETUP_JAVASCRIPT)
        page.get_by_text('QQ 已掉线，需要重新扫码登录', exact=True).wait_for()
        assert page.get_by_alt_text('QQ 登录二维码').is_visible()
        page.evaluate('() => { window.loggedIn = true; }')  # the scan went through; the next 2 s poll sees it
        page.get_by_text('QQ 已登录，OneBot 已就绪 · v4.18.28', exact=True).wait_for()
        assert page.get_by_text('已连接', exact=True).is_visible()
        assert '接收连接已建立' in page.get_by_role('status').inner_text()
        assert page.get_by_alt_text('QQ 登录二维码').count() == 0
        assert page.get_by_text('QQ 已掉线，需要重新扫码登录').count() == 0
        assert page.get_by_text('state is not defined').count() == 0
        assert errors == []
        page.screenshot(path=str(tmp_path / 'qq-rescan-connected.png'), full_page=True)
        browser.close()


@pytest.mark.parametrize("outcome", ["READY_RESTART", "CONNECTING", "FAILED"])
def test_qq_save_keeps_draft_and_reports_actual_outcome(outcome, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        page.set_content(PAGE)
        page.evaluate('''() => {
          window.saved = false;
          window.fetch = async (url, options) => {
            if (String(url).endsWith('/configure')) return new Promise(resolve => { window.finishSave = () => {
              window.saved = window.outcome !== 'FAILED';
              resolve({ok: window.saved, json: async () => window.saved ? {status:window.outcome} : {error:'QQ_LOGIN_UNAVAILABLE'}});
            }; });
            return {ok:true, json:async () => ({selected_channels:['qq'], configured:{qq:window.saved},
              listeners:{qq:window.saved ? window.outcome === 'CONNECTING' ? 'CONNECTED' : 'CONFIGURED_RESTART' : 'SETUP_REQUIRED'},
              qq:window.saved ? {state:window.outcome,owner_masked:'*****4321'} : {state:'IDLE'},
              napcat:{state:'ONEBOT_READY',installed:true}})};
          };
        }''')
        page.evaluate('(v) => window.outcome=v', outcome)
        page.evaluate(MOUNT, PERSONAL_CHAT_SETUP_JAVASCRIPT)
        owner = page.get_by_label('你自己的 QQ 号')
        owner.fill('987654321')
        page.get_by_role('button', name='保存', exact=True).click()
        assert page.get_by_role('button', name='正在保存…').is_disabled()
        assert owner.is_disabled()
        page.evaluate('() => window.finishSave()')
        if outcome == 'FAILED':
            page.get_by_role('button', name='保存', exact=True).wait_for()
            assert owner.input_value() == '987654321' and owner.is_enabled()
            assert page.get_by_text('QQ_LOGIN_UNAVAILABLE', exact=True).is_visible()
            assert page.get_by_role('status').count() == 0
        else:
            feedback = page.get_by_role('status')
            feedback.wait_for()
            text = feedback.inner_text()
            assert '你的 QQ：*****4321' in text
            assert ('重启 Olivia 后生效' in text) == (outcome == 'READY_RESTART')
        page.screenshot(path=str(tmp_path / f'qq-save-{outcome}.png'), full_page=True)
        browser.close()
